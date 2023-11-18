"""Utilities for assertion debugging."""
import collections.abc
import os
import pprint
from typing import AbstractSet
from typing import Any
from typing import Callable
from typing import Iterable
from typing import List
from typing import Literal
from typing import Mapping
from typing import Optional
from typing import Protocol
from typing import Sequence
from unicodedata import normalize

import _pytest._code
from _pytest import outcomes
from _pytest._io.pprint import PrettyPrinter
from _pytest._io.saferepr import saferepr
from _pytest._io.saferepr import saferepr_unlimited
from _pytest.config import Config

# The _reprcompare attribute on the util module is used by the new assertion
# interpretation code and assertion rewriter to detect this plugin was
# loaded and in turn call the hooks defined here as part of the
# DebugInterpreter.
_reprcompare: Optional[Callable[[str, object, object], Optional[str]]] = None

# Works similarly as _reprcompare attribute. Is populated with the hook call
# when pytest_runtest_setup is called.
_assertion_pass: Optional[Callable[[int, str, str], None]] = None

# Config object which is assigned during pytest_runtest_protocol.
_config: Optional[Config] = None


class _HighlightFunc(Protocol):
    def __call__(self, source: str, lexer: Literal["diff", "python"] = "python") -> str:
        """Apply highlighting to the given source."""


def format_explanation(explanation: str) -> str:
    r"""Format an explanation.

    Normally all embedded newlines are escaped, however there are
    three exceptions: \n{, \n} and \n~.  The first two are intended
    cover nested explanations, see function and attribute explanations
    for examples (.visit_Call(), visit_Attribute()).  The last one is
    for when one explanation needs to span multiple lines, e.g. when
    displaying diffs.
    """
    lines = _split_explanation(explanation)
    result = _format_lines(lines)
    return "\n".join(result)


def _split_explanation(explanation: str) -> List[str]:
    r"""Return a list of individual lines in the explanation.

    This will return a list of lines split on '\n{', '\n}' and '\n~'.
    Any other newlines will be escaped and appear in the line as the
    literal '\n' characters.
    """
    raw_lines = (explanation or "").split("\n")
    lines = [raw_lines[0]]
    for values in raw_lines[1:]:
        if values and values[0] in ["{", "}", "~", ">"]:
            lines.append(values)
        else:
            lines[-1] += "\\n" + values
    return lines


def _format_lines(lines: Sequence[str]) -> List[str]:
    """Format the individual lines.

    This will replace the '{', '}' and '~' characters of our mini formatting
    language with the proper 'where ...', 'and ...' and ' + ...' text, taking
    care of indentation along the way.

    Return a list of formatted lines.
    """
    result = list(lines[:1])
    stack = [0]
    stackcnt = [0]
    for line in lines[1:]:
        if line.startswith("{"):
            if stackcnt[-1]:
                s = "and   "
            else:
                s = "where "
            stack.append(len(result))
            stackcnt[-1] += 1
            stackcnt.append(0)
            result.append(" +" + "  " * (len(stack) - 1) + s + line[1:])
        elif line.startswith("}"):
            stack.pop()
            stackcnt.pop()
            result[stack[-1]] += line[1:]
        else:
            assert line[0] in ["~", ">"]
            stack[-1] += 1
            indent = len(stack) if line.startswith("~") else len(stack) - 1
            result.append("  " * indent + line[1:])
    assert len(stack) == 1
    return result


def issequence(x: Any) -> bool:
    return isinstance(x, collections.abc.Sequence) and not isinstance(x, str)


def istext(x: Any) -> bool:
    return isinstance(x, str)


def isdict(x: Any) -> bool:
    return isinstance(x, dict)


def isset(x: Any) -> bool:
    return isinstance(x, (set, frozenset))


def isnamedtuple(obj: Any) -> bool:
    return isinstance(obj, tuple) and getattr(obj, "_fields", None) is not None


def isdatacls(obj: Any) -> bool:
    return getattr(obj, "__dataclass_fields__", None) is not None


def isattrs(obj: Any) -> bool:
    return getattr(obj, "__attrs_attrs__", None) is not None


def isiterable(obj: Any) -> bool:
    try:
        iter(obj)
        return not istext(obj)
    except Exception:
        return False


def has_default_eq(
    obj: object,
) -> bool:
    """Check if an instance of an object contains the default eq

    First, we check if the object's __eq__ attribute has __code__,
    if so, we check the equally of the method code filename (__code__.co_filename)
    to the default one generated by the dataclass and attr module
    for dataclasses the default co_filename is <string>, for attrs class, the __eq__ should contain "attrs eq generated"
    """
    # inspired from https://github.com/willmcgugan/rich/blob/07d51ffc1aee6f16bd2e5a25b4e82850fb9ed778/rich/pretty.py#L68
    if hasattr(obj.__eq__, "__code__") and hasattr(obj.__eq__.__code__, "co_filename"):
        code_filename = obj.__eq__.__code__.co_filename

        if isattrs(obj):
            return "attrs generated eq" in code_filename

        return code_filename == "<string>"  # data class
    return True


def assertrepr_compare(
    config, op: str, left: Any, right: Any, use_ascii: bool = False
) -> Optional[List[str]]:
    """Return specialised explanations for some operators/operands."""
    verbose = config.get_verbosity(Config.VERBOSITY_ASSERTIONS)

    # Strings which normalize equal are often hard to distinguish when printed; use ascii() to make this easier.
    # See issue #3246.
    use_ascii = (
        isinstance(left, str)
        and isinstance(right, str)
        and normalize("NFD", left) == normalize("NFD", right)
    )

    if verbose > 1:
        left_repr = saferepr_unlimited(left, use_ascii=use_ascii)
        right_repr = saferepr_unlimited(right, use_ascii=use_ascii)
    else:
        # XXX: "15 chars indentation" is wrong
        #      ("E       AssertionError: assert "); should use term width.
        maxsize = (
            80 - 15 - len(op) - 2
        ) // 2  # 15 chars indentation, 1 space around op

        left_repr = saferepr(left, maxsize=maxsize, use_ascii=use_ascii)
        right_repr = saferepr(right, maxsize=maxsize, use_ascii=use_ascii)

    summary = f"{left_repr} {op} {right_repr}"

    explanation = None
    try:
        if op == "==":
            writer = config.get_terminal_writer()
            explanation = _compare_eq_any(left, right, writer._highlight, verbose)
        elif op == "not in":
            if istext(left) and istext(right):
                explanation = _notin_text(left, right, verbose)
        elif op == "!=":
            if isset(left) and isset(right):
                explanation = ["Both sets are equal"]
        elif op == ">=":
            if isset(left) and isset(right):
                explanation = _compare_gte_set(left, right, verbose)
        elif op == "<=":
            if isset(left) and isset(right):
                explanation = _compare_lte_set(left, right, verbose)
        elif op == ">":
            if isset(left) and isset(right):
                explanation = _compare_gt_set(left, right, verbose)
        elif op == "<":
            if isset(left) and isset(right):
                explanation = _compare_lt_set(left, right, verbose)

    except outcomes.Exit:
        raise
    except Exception:
        explanation = [
            "(pytest_assertion plugin: representation of details failed: {}.".format(
                _pytest._code.ExceptionInfo.from_current()._getreprcrash()
            ),
            " Probably an object has a faulty __repr__.)",
        ]

    if not explanation:
        return None

    return [summary] + explanation


def _compare_eq_any(
    left: Any, right: Any, highlighter: _HighlightFunc, verbose: int = 0
) -> List[str]:
    explanation = []
    if istext(left) and istext(right):
        explanation = _diff_text(left, right, verbose)
    else:
        from _pytest.python_api import ApproxBase

        if isinstance(left, ApproxBase) or isinstance(right, ApproxBase):
            # Although the common order should be obtained == expected, this ensures both ways
            approx_side = left if isinstance(left, ApproxBase) else right
            other_side = right if isinstance(left, ApproxBase) else left

            explanation = approx_side._repr_compare(other_side)
        elif type(left) is type(right) and (
            isdatacls(left) or isattrs(left) or isnamedtuple(left)
        ):
            # Note: unlike dataclasses/attrs, namedtuples compare only the
            # field values, not the type or field names. But this branch
            # intentionally only handles the same-type case, which was often
            # used in older code bases before dataclasses/attrs were available.
            explanation = _compare_eq_cls(left, right, highlighter, verbose)
        elif issequence(left) and issequence(right):
            explanation = _compare_eq_sequence(left, right, verbose)
        elif isset(left) and isset(right):
            explanation = _compare_eq_set(left, right, verbose)
        elif isdict(left) and isdict(right):
            explanation = _compare_eq_dict(left, right, verbose)

        if isiterable(left) and isiterable(right):
            expl = _compare_eq_iterable(left, right, highlighter, verbose)
            explanation.extend(expl)

    return explanation


def _diff_text(left: str, right: str, verbose: int = 0) -> List[str]:
    """Return the explanation for the diff between text.

    Unless --verbose is used this will skip leading and trailing
    characters which are identical to keep the diff minimal.
    """
    from difflib import ndiff

    explanation: List[str] = []

    if verbose < 1:
        i = 0  # just in case left or right has zero length
        for i in range(min(len(left), len(right))):
            if left[i] != right[i]:
                break
        if i > 42:
            i -= 10  # Provide some context
            explanation = [
                "Skipping %s identical leading characters in diff, use -v to show" % i
            ]
            left = left[i:]
            right = right[i:]
        if len(left) == len(right):
            for i in range(len(left)):
                if left[-i] != right[-i]:
                    break
            if i > 42:
                i -= 10  # Provide some context
                explanation += [
                    "Skipping {} identical trailing "
                    "characters in diff, use -v to show".format(i)
                ]
                left = left[:-i]
                right = right[:-i]
    keepends = True
    if left.isspace() or right.isspace():
        left = repr(str(left))
        right = repr(str(right))
        explanation += ["Strings contain only whitespace, escaping them using repr()"]
    # "right" is the expected base against which we compare "left",
    # see https://github.com/pytest-dev/pytest/issues/3333
    explanation += [
        line.strip("\n")
        for line in ndiff(right.splitlines(keepends), left.splitlines(keepends))
    ]
    return explanation


def _surrounding_parens_on_own_lines(lines: List[str]) -> None:
    """Move opening/closing parenthesis/bracket to own lines."""
    opening = lines[0][:1]
    if opening in ["(", "[", "{"]:
        lines[0] = " " + lines[0][1:]
        lines[:] = [opening] + lines
    closing = lines[-1][-1:]
    if closing in [")", "]", "}"]:
        lines[-1] = lines[-1][:-1] + ","
        lines[:] = lines + [closing]


def _compare_eq_iterable(
    left: Iterable[Any],
    right: Iterable[Any],
    highligher: _HighlightFunc,
    verbose: int = 0,
) -> List[str]:
    if verbose <= 0 and not running_on_ci():
        return ["Use -v to get more diff"]
    # dynamic import to speedup pytest
    import difflib

    left_formatting = pprint.pformat(left).splitlines()
    right_formatting = pprint.pformat(right).splitlines()

    # Re-format for different output lengths.
    lines_left = len(left_formatting)
    lines_right = len(right_formatting)
    if lines_left != lines_right:
        printer = PrettyPrinter()
        left_formatting = printer.pformat(left).splitlines()
        right_formatting = printer.pformat(right).splitlines()

    if lines_left > 1 or lines_right > 1:
        _surrounding_parens_on_own_lines(left_formatting)
        _surrounding_parens_on_own_lines(right_formatting)

    explanation = ["Full diff:"]
    # "right" is the expected base against which we compare "left",
    # see https://github.com/pytest-dev/pytest/issues/3333
    explanation.extend(
        highligher(
            "\n".join(
                line.rstrip()
                for line in difflib.ndiff(right_formatting, left_formatting)
            ),
            lexer="diff",
        ).splitlines()
    )
    return explanation


def _compare_eq_sequence(
    left: Sequence[Any], right: Sequence[Any], verbose: int = 0
) -> List[str]:
    comparing_bytes = isinstance(left, bytes) and isinstance(right, bytes)
    explanation: List[str] = []
    len_left = len(left)
    len_right = len(right)
    for i in range(min(len_left, len_right)):
        if left[i] != right[i]:
            if comparing_bytes:
                # when comparing bytes, we want to see their ascii representation
                # instead of their numeric values (#5260)
                # using a slice gives us the ascii representation:
                # >>> s = b'foo'
                # >>> s[0]
                # 102
                # >>> s[0:1]
                # b'f'
                left_value = left[i : i + 1]
                right_value = right[i : i + 1]
            else:
                left_value = left[i]
                right_value = right[i]

            explanation += [f"At index {i} diff: {left_value!r} != {right_value!r}"]
            break

    if comparing_bytes:
        # when comparing bytes, it doesn't help to show the "sides contain one or more
        # items" longer explanation, so skip it

        return explanation

    len_diff = len_left - len_right
    if len_diff:
        if len_diff > 0:
            dir_with_more = "Left"
            extra = saferepr(left[len_right])
        else:
            len_diff = 0 - len_diff
            dir_with_more = "Right"
            extra = saferepr(right[len_left])

        if len_diff == 1:
            explanation += [f"{dir_with_more} contains one more item: {extra}"]
        else:
            explanation += [
                "%s contains %d more items, first extra item: %s"
                % (dir_with_more, len_diff, extra)
            ]
    return explanation


def _compare_eq_set(
    left: AbstractSet[Any], right: AbstractSet[Any], verbose: int = 0
) -> List[str]:
    explanation = []
    explanation.extend(_set_one_sided_diff("left", left, right))
    explanation.extend(_set_one_sided_diff("right", right, left))
    return explanation


def _compare_gt_set(
    left: AbstractSet[Any], right: AbstractSet[Any], verbose: int = 0
) -> List[str]:
    explanation = _compare_gte_set(left, right, verbose)
    if not explanation:
        return ["Both sets are equal"]
    return explanation


def _compare_lt_set(
    left: AbstractSet[Any], right: AbstractSet[Any], verbose: int = 0
) -> List[str]:
    explanation = _compare_lte_set(left, right, verbose)
    if not explanation:
        return ["Both sets are equal"]
    return explanation


def _compare_gte_set(
    left: AbstractSet[Any], right: AbstractSet[Any], verbose: int = 0
) -> List[str]:
    return _set_one_sided_diff("right", right, left)


def _compare_lte_set(
    left: AbstractSet[Any], right: AbstractSet[Any], verbose: int = 0
) -> List[str]:
    return _set_one_sided_diff("left", left, right)


def _set_one_sided_diff(
    posn: str, set1: AbstractSet[Any], set2: AbstractSet[Any]
) -> List[str]:
    explanation = []
    diff = set1 - set2
    if diff:
        explanation.append(f"Extra items in the {posn} set:")
        for item in diff:
            explanation.append(saferepr(item))
    return explanation


def _compare_eq_dict(
    left: Mapping[Any, Any], right: Mapping[Any, Any], verbose: int = 0
) -> List[str]:
    explanation: List[str] = []
    set_left = set(left)
    set_right = set(right)
    common = set_left.intersection(set_right)
    same = {k: left[k] for k in common if left[k] == right[k]}
    if same and verbose < 2:
        explanation += ["Omitting %s identical items, use -vv to show" % len(same)]
    elif same:
        explanation += ["Common items:"]
        explanation += pprint.pformat(same).splitlines()
    diff = {k for k in common if left[k] != right[k]}
    if diff:
        explanation += ["Differing items:"]
        for k in diff:
            explanation += [saferepr({k: left[k]}) + " != " + saferepr({k: right[k]})]
    extra_left = set_left - set_right
    len_extra_left = len(extra_left)
    if len_extra_left:
        explanation.append(
            "Left contains %d more item%s:"
            % (len_extra_left, "" if len_extra_left == 1 else "s")
        )
        explanation.extend(
            pprint.pformat({k: left[k] for k in extra_left}).splitlines()
        )
    extra_right = set_right - set_left
    len_extra_right = len(extra_right)
    if len_extra_right:
        explanation.append(
            "Right contains %d more item%s:"
            % (len_extra_right, "" if len_extra_right == 1 else "s")
        )
        explanation.extend(
            pprint.pformat({k: right[k] for k in extra_right}).splitlines()
        )
    return explanation


def _compare_eq_cls(
    left: Any, right: Any, highlighter: _HighlightFunc, verbose: int
) -> List[str]:
    if not has_default_eq(left):
        return []
    if isdatacls(left):
        import dataclasses

        all_fields = dataclasses.fields(left)
        fields_to_check = [info.name for info in all_fields if info.compare]
    elif isattrs(left):
        all_fields = left.__attrs_attrs__
        fields_to_check = [field.name for field in all_fields if getattr(field, "eq")]
    elif isnamedtuple(left):
        fields_to_check = left._fields
    else:
        assert False

    indent = "  "
    same = []
    diff = []
    for field in fields_to_check:
        if getattr(left, field) == getattr(right, field):
            same.append(field)
        else:
            diff.append(field)

    explanation = []
    if same or diff:
        explanation += [""]
    if same and verbose < 2:
        explanation.append("Omitting %s identical items, use -vv to show" % len(same))
    elif same:
        explanation += ["Matching attributes:"]
        explanation += pprint.pformat(same).splitlines()
    if diff:
        explanation += ["Differing attributes:"]
        explanation += pprint.pformat(diff).splitlines()
        for field in diff:
            field_left = getattr(left, field)
            field_right = getattr(right, field)
            explanation += [
                "",
                "Drill down into differing attribute %s:" % field,
                ("%s%s: %r != %r") % (indent, field, field_left, field_right),
            ]
            explanation += [
                indent + line
                for line in _compare_eq_any(
                    field_left, field_right, highlighter, verbose
                )
            ]
    return explanation


def _notin_text(term: str, text: str, verbose: int = 0) -> List[str]:
    index = text.find(term)
    head = text[:index]
    tail = text[index + len(term) :]
    correct_text = head + tail
    diff = _diff_text(text, correct_text, verbose)
    newdiff = ["%s is contained here:" % saferepr(term, maxsize=42)]
    for line in diff:
        if line.startswith("Skipping"):
            continue
        if line.startswith("- "):
            continue
        if line.startswith("+ "):
            newdiff.append("  " + line[2:])
        else:
            newdiff.append(line)
    return newdiff


def running_on_ci() -> bool:
    """Check if we're currently running on a CI system."""
    env_vars = ["CI", "BUILD_NUMBER"]
    return any(var in os.environ for var in env_vars)
