import gc
import http.client
import json
import socket
import threading
import time
import unittest
import warnings
from unittest import mock

from jobscan.connections import MAX_REDIRECTS, Connections
from tests.fakes import FakeServer


def wait_until(condition, seconds=5):
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition never held")
        time.sleep(0.01)


class ConnectionsTest(unittest.TestCase):
    def setUp(self):
        self.server = self.started(keep_alive=True)
        for name in ("a", "b", "c"):
            self.server.routes[f"/{name}"] = (200, {"page": name})
        self.connections = Connections(5)
        self.addCleanup(self.connections.close)

    def started(self, keep_alive):
        server = FakeServer(keep_alive=keep_alive)
        self.addCleanup(server.close)
        return server

    def send(self, path, body=None, server=None, headers=None):
        return self.connections.send(f"{(server or self.server).url}{path}", body, headers or {})

    def page(self, answer):
        return answer.status, json.loads(answer.body)

    def test_one_thread_keeps_its_connection_for_the_next_request(self):
        pages = [self.page(self.send(f"/{name}")) for name in ("a", "b", "c")]
        self.assertEqual(pages, [(200, {"page": name}) for name in ("a", "b", "c")])
        self.assertEqual(len(set(self.server.client_ports)), 1)

    def test_another_host_closes_and_replaces_the_kept_connection(self):
        other = self.started(keep_alive=True)
        other.routes["/x"] = (200, {"page": "x"})
        pages = [self.page(self.send("/a")), self.page(self.send("/x", server=other)), self.page(self.send("/b"))]
        self.assertEqual(pages, [(200, {"page": "a"}), (200, {"page": "x"}), (200, {"page": "b"})])
        self.assertEqual((len(set(self.server.client_ports)), len(set(other.client_ports))), (2, 1))
        wait_until(lambda: self.server.closed == 1 and other.closed == 1)  # each left when the thread moved on

    def test_each_thread_keeps_its_own(self):
        threads, rounds, pages = 4, 5, []
        together = threading.Barrier(threads, timeout=5)

        def work():
            together.wait()
            for _ in range(rounds):
                pages.append(self.page(self.send("/a")))

        workers = [threading.Thread(target=work) for _ in range(threads)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(pages, [(200, {"page": "a"})] * threads * rounds)
        self.assertEqual(len(set(self.server.client_ports)), threads)

    def test_a_server_closing_every_answer_gets_a_new_connection_each_time(self):
        plain = self.started(keep_alive=False)
        plain.routes["/a"] = (200, {"page": "a"})
        self.assertEqual([self.page(self.send("/a", server=plain)) for _ in range(3)], [(200, {"page": "a"})] * 3)
        self.assertEqual(len(set(plain.client_ports)), 3)

    def test_an_answer_other_than_200_is_not_kept_and_its_body_not_read(self):
        missing = self.send("/missing")
        self.assertEqual((missing.status, missing.body), (404, b""))
        self.assertEqual(self.page(self.send("/a")), (200, {"page": "a"}))
        self.assertEqual(len(set(self.server.client_ports)), 2)
        self.server.routes["/stuck"] = (503, b'{"error": "bu')  # its body never ends: reading it would time out
        self.server.stall.add("/stuck")
        self.addCleanup(self.server.released.set)
        self.assertEqual(Connections(0.2).send(f"{self.server.url}/stuck", None, {}).status, 503)

    def test_a_kept_connection_the_server_dropped_is_sent_once_more_on_a_new_one(self):
        self.server.drop_after.add("/a")
        self.server.routes["/echo"] = (200, lambda payload: (200, {"echo": payload}))
        self.send("/a")
        self.assertEqual(self.page(self.send("/b")), (200, {"page": "b"}))
        self.server.drop_after.add("/b")
        self.send("/b")
        posted = self.send("/echo", b'{"limit": 20}', headers={"Content-Type": "application/json"})
        self.assertEqual(self.page(posted), (200, {"echo": {"limit": 20}}))
        self.assertEqual(self.server.requests[-1], ("POST", "/echo", {"limit": 20}))
        self.assertEqual(self.server.content_types[-1], ("POST", "/echo", "application/json"))
        self.assertEqual(len(set(self.server.client_ports)), 3)

    def test_a_resend_that_fails_too_raises_after_one_new_connection(self):
        self.server.drop_after.add("/a")
        self.server.hang_up.add("/b")
        self.send("/a")
        with self.assertRaises(ConnectionError):
            self.send("/b")
        self.assertEqual([path for _, path, _ in self.server.requests], ["/a", "/b"])
        self.assertEqual(len(set(self.server.client_ports)), 2)

    def test_a_new_connections_failure_is_not_sent_again(self):
        self.server.hang_up.add("/b")
        with self.assertRaises(ConnectionError):
            self.send("/b")
        self.assertEqual([path for _, path, _ in self.server.requests], ["/b"])
        nobody = Connections(5)
        with self.assertRaises(ConnectionRefusedError):
            nobody.send("http://127.0.0.1:9/a", None, {})

    def test_a_cut_200_raises_and_its_connection_is_not_kept(self):
        self.server.routes["/cut"] = (200, b'{"page": "cu')
        self.send("/a")
        with self.assertRaises(http.client.IncompleteRead):
            self.send("/cut")
        self.assertEqual(self.page(self.send("/b")), (200, {"page": "b"}))
        self.assertEqual(len(self.server.client_ports), 3)
        self.assertEqual(len(set(self.server.client_ports)), 2)

    def test_a_200_from_a_server_closing_every_answer_that_times_out_closes_its_socket(self):
        plain = self.started(keep_alive=False)  # the response, not the connection, holds the socket
        plain.routes["/slow"] = (200, b'{"page": "sl')
        plain.stall.add("/slow")
        self.addCleanup(plain.released.set)
        made, dial = [], socket.create_connection

        def dialled(*args, **kwargs):
            made.append(dial(*args, **kwargs))
            return made[-1]

        with mock.patch("socket.create_connection", dialled):
            try:
                Connections(0.2).send(f"{plain.url}/slow", None, {})
            except TimeoutError as e:
                kept = e  # its traceback keeps everything send held, so nothing is closed by collection
        self.assertEqual([sock.fileno() for sock in made], [-1])
        del kept

    def test_an_ipv6_host_is_asked_on_the_port_its_url_names_or_its_schemes(self):
        for url, address in [("http://[::1]/a", ("::1", 80)), ("https://[2001:db8::abcd]/a", ("2001:db8::abcd", 443)),
                             ("http://[::1]:8080/a", ("::1", 8080))]:
            with self.subTest(url), mock.patch("socket.create_connection", side_effect=ConnectionRefusedError) as dial:
                with self.assertRaises(ConnectionRefusedError):
                    self.connections.send(url, None, {})
                self.assertEqual(dial.call_args.args[0], address)

    def test_a_gets_redirects_are_followed(self):
        other = self.started(keep_alive=True)
        other.routes["/x"] = (200, {"page": "x"})
        self.server.routes["/absolute"] = (302, {}, {"Location": f"{self.server.url}/a"})
        self.server.routes["/relative"] = (301, {}, {"Location": "/b?q=1"})
        self.server.routes["/b?q=1"] = (200, {"page": "b with q"})
        self.server.routes["/elsewhere"] = (307, {}, {"Location": f"{other.url}/x"})
        self.assertEqual([self.page(self.send(path)) for path in ("/absolute", "/relative", "/elsewhere")],
                         [(200, {"page": "a"}), (200, {"page": "b with q"}), (200, {"page": "x"})])

    def test_a_location_is_percent_encoded_as_urllib_does(self):
        utf8 = "/caf\u00e9?q=a b".encode().decode("latin-1")  # header bytes, as http.client reads them: Latin-1
        self.server.routes["/spaced"] = (302, {}, {"Location": "/with space/x%41"})
        self.server.routes["/with%20space/x%41"] = (200, {"page": "spaced"})
        self.server.routes["/accented"] = (302, {}, {"Location": utf8})
        self.server.routes["/caf%C3%A9?q=a%20b"] = (200, {"page": "accented"})
        self.assertEqual([self.page(self.send(path)) for path in ("/spaced", "/accented")],
                         [(200, {"page": "spaced"}), (200, {"page": "accented"})])

    def test_redirects_not_followed_are_the_answer(self):
        self.server.routes["/moved"] = (302, {}, {"Location": "/a"})
        self.server.routes["/nowhere"] = (302, {})
        self.server.routes["/ftp"] = (301, {}, {"Location": "ftp://127.0.0.1/a"})
        self.server.routes["/loop"] = (302, {}, {"Location": "/loop"})
        self.server.routes["/badport"] = (302, {}, {"Location": "http://127.0.0.1:99999/a"})
        self.server.routes["/badhost"] = (302, {}, {"Location": "http://[::1/a"})
        self.assertEqual(self.send("/moved", b"{}").status, 302)  # a POST's redirect
        self.assertEqual([self.send(path).status for path in ("/nowhere", "/ftp", "/loop", "/badport", "/badhost")],
                         [302, 301, 302, 302, 302])
        self.assertEqual(sum(path == "/loop" for _, path, _ in self.server.requests), MAX_REDIRECTS + 1)
        self.assertNotIn("/a", [path for _, path, _ in self.server.requests])

    def test_only_http_urls_are_sent(self):
        for url in ["ftp://127.0.0.1/a", "http:///a", "/a", "http://127.0.0.1:99999/a", "http://[::1/a"]:
            with self.assertRaises(ValueError, msg=url):
                self.connections.send(url, None, {})

    def test_close_ends_every_kept_connection(self):
        together = threading.Barrier(3, timeout=5)  # three live threads: an ended one's id can be reused

        def work():
            together.wait()
            self.send("/a")

        workers = [threading.Thread(target=work) for _ in range(3)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual((len(set(self.server.client_ports)), self.server.closed), (3, 0))
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            self.connections.close()
            gc.collect()
        self.assertEqual([w for w in caught if issubclass(w.category, ResourceWarning)], [])  # closed, not dropped
        wait_until(lambda: self.server.closed == 3)


if __name__ == "__main__":
    unittest.main()
