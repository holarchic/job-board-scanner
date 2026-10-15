"""The help text keeps up with the flags: an agent sets an install up from `--help` alone."""
import argparse
import re
import unittest

from jobscan import arguments


def subcommands(parser: argparse.ArgumentParser, prefix: str = "jobscan") -> dict[str, argparse.ArgumentParser]:
    """Every command, nested ones included (`jobscan boards add`), by its full invocation."""
    found = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                found[f"{prefix} {name}"] = sub
                found |= subcommands(sub, f"{prefix} {name}")
    return found


class HelpTest(unittest.TestCase):
    def test_every_command_has_examples_using_each_of_its_flags(self):
        commands = subcommands(arguments.parser())
        self.assertIn("jobscan boards add", commands)
        for invocation, parser in commands.items():
            examples = parser.epilog.split("Examples:\n", 1)[1]
            self.assertIn(f"  {invocation}", examples, invocation)
            flags = {o for a in parser._actions for o in a.option_strings if o not in ("-h", "--help")}
            for flag in flags:
                self.assertRegex(examples, rf"{re.escape(flag)}(?![\w-])", f"{invocation}: no example uses {flag}")

    def test_the_set_up_steps_name_real_commands_in_help_order(self):
        steps = re.findall(r"^  \d\. jobscan (\w+)", arguments.SET_UP, re.MULTILINE)
        self.assertEqual(steps, ["init", "email", "boards", "check", "run", "preview", "schedule"])
        names = [n.split()[1] for n in subcommands(arguments.parser()) if n.count(" ") == 1]
        self.assertEqual(names[:7], ["init", "email", "boards", "check", "run", "preview", "schedule"])

    def test_no_flag_takes_a_secret(self):
        for invocation, parser in subcommands(arguments.parser()).items():
            for action in parser._actions:
                for flag in action.option_strings:
                    if re.search(r"key|password|secret|token", flag):
                        self.assertIsInstance(action, argparse._StoreTrueAction, f"{invocation} {flag}")


if __name__ == "__main__":
    unittest.main()
