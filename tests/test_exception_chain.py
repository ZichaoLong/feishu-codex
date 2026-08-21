import unittest

from bot.exception_chain import iter_exception_chain


class ExceptionChainTests(unittest.TestCase):
    def test_explicit_cause_takes_precedence(self) -> None:
        outer = RuntimeError("outer")
        cause = ValueError("cause")
        context = LookupError("context")
        outer.__cause__ = cause
        outer.__context__ = context

        self.assertEqual(list(iter_exception_chain(outer)), [outer, cause])

    def test_implicit_context_is_followed_without_a_cause(self) -> None:
        outer = RuntimeError("outer")
        context = ValueError("context")
        outer.__context__ = context

        self.assertEqual(list(iter_exception_chain(outer)), [outer, context])

    def test_cycle_yields_each_exception_once(self) -> None:
        first = RuntimeError("first")
        second = ValueError("second")
        first.__cause__ = second
        second.__context__ = first

        self.assertEqual(list(iter_exception_chain(first)), [first, second])


if __name__ == "__main__":
    unittest.main()
