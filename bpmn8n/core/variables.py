"""Process variables: the data of one case (process instance), shared by all its steps.

A case starts with the data that started it (a webhook's body) and gains every task result,
every completed form and every received message. Node fields refer to them as {{name}}:
- "{{book}}" alone keeps the variable's type (a number stays a number);
- inside text it is filled in: "Your book {{book}} is ready";
- dots reach nested data and list items: {{customer.email}}, {{hosts.0}};
- a task field left empty takes the variable with the same name (binding by name).
Gateway conditions read like `{{amount}} > 1000 and not {{vip}}`."""
import ast
import operator
import re

TEMPLATE = re.compile(r"\{\{\s*([A-Za-z_]\w*(?:\.\w+)*)\s*\}\}")
MISSING = object()


def lookup(variables, path):
    value = variables
    for part in path.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return MISSING
    return value


def render(value, variables):
    """Fills the {{name}} templates in `value` (recursively). A lone template is replaced by the
    variable itself, MISSING when it does not exist; unknown names inside text become ""."""
    if isinstance(value, str):
        if whole := TEMPLATE.fullmatch(value.strip()):
            return lookup(variables, whole.group(1))
        return TEMPLATE.sub(lambda m: "" if (v := lookup(variables, m.group(1))) is MISSING else str(v), value)
    if isinstance(value, dict):
        return {k: v for k, v in ((k, render(v, variables)) for k, v in value.items()) if v is not MISSING}
    if isinstance(value, list):
        return [v for v in (render(v, variables) for v in value) if v is not MISSING]
    return value


def bind(schema, given, variables, extracted=None):
    """A task's input, in order of precedence: the fields set on the node (templates filled),
    the values extracted from text, the variables named like the remaining fields."""
    filled = {k: v for k, v in render(given, variables).items() if v not in ("", None)}
    by_name = {name: variables[name] for name in schema.model_fields if name in variables}
    return by_name | (extracted or {}) | filled


WEBHOOK_ENVELOPE = ("headers", "params", "query", "body", "webhookUrl", "executionMode")


def from_item(item):
    """The variables an n8n item brings: a webhook item's body plus what later steps set next to it
    (a task's merged output, a sub-process's results), which wins over the body; else the item itself.
    Keys starting with "_" are bpmn8n's bookkeeping, not process data."""
    if isinstance(item.get("body"), dict) and "headers" in item:
        data = {**item["body"], **{k: v for k, v in item.items() if k not in WEBHOOK_ENVELOPE}}
    else:
        data = item
    return {k: v for k, v in data.items() if not k.startswith("_")}


class ConditionError(ValueError):
    pass


class Condition:
    """A gateway condition, walked by its own small evaluator (no eval): only comparisons,
    and/or/not, literals and {{variables}} (no calls, attributes, indexing or arithmetic)."""

    WORDS = {"true": True, "false": False, "null": None}  # JSON spelling, as in n8n
    COMPARE = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le,
               ast.Gt: operator.gt, ast.GtE: operator.ge, ast.In: lambda a, b: a in b,
               ast.NotIn: lambda a, b: a not in b}
    ALLOWED = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.Compare,
               ast.Constant, ast.Name, ast.Load, ast.List, ast.Tuple, *COMPARE)

    @classmethod
    def parse(cls, condition):
        """(syntax tree, {variable path: placeholder}); ConditionError if it is not allowed."""
        names = {}
        source = TEMPLATE.sub(lambda m: names.setdefault(m.group(1), f"_v{len(names)}"), str(condition))
        try:
            tree = ast.parse(source.strip() or "False", mode="eval")
        except SyntaxError as e:
            raise ConditionError(f"cannot read condition {condition!r}") from e
        for node in ast.walk(tree):
            if not isinstance(node, cls.ALLOWED) or isinstance(node, ast.Name) and node.id not in {*names.values(), *cls.WORDS}:
                raise ConditionError(f"not allowed in a condition: {ast.unparse(node)!r} (in {condition!r})")
        return tree, names

    @classmethod
    def holds(cls, condition, variables):
        if isinstance(condition, bool):  # an n8n expression already evaluated by n8n
            return condition
        tree, names = cls.parse(condition)
        env = cls.WORDS | {placeholder: (None if (v := lookup(variables, path)) is MISSING else v)
                           for path, placeholder in names.items()}
        try:
            return bool(cls._value(tree.body, env))
        except TypeError as e:  # e.g. None > 1000: a variable the case does not have
            raise ConditionError(f"{condition!r}: {e}") from e

    @classmethod
    def _value(cls, node, env):
        match node:
            case ast.BoolOp(op=ast.And(), values=values):
                return all(cls._value(v, env) for v in values)
            case ast.BoolOp(values=values):  # or
                return any(cls._value(v, env) for v in values)
            case ast.UnaryOp(op=ast.Not(), operand=operand):
                return not cls._value(operand, env)
            case ast.UnaryOp(operand=operand):  # minus
                return -cls._value(operand, env)
            case ast.Compare(left=left, ops=ops, comparators=rights):
                a = cls._value(left, env)
                for op, right in zip(ops, rights):
                    b = cls._value(right, env)
                    if not cls.COMPARE[type(op)](*comparable(a, b)):
                        return False
                    a = b
                return True
            case ast.Constant(value=value):
                return value
            case ast.Name(id=name):
                return env[name]
            case ast.List(elts=items) | ast.Tuple(elts=items):
                return [cls._value(i, env) for i in items]


def comparable(a, b):
    """A number that arrives as text (a web form's "2400") compares as a number with a number,
    == included; text compared with text stays text ("00123" == "00123")."""
    def number(x):
        return isinstance(x, (int, float)) and not isinstance(x, bool)

    def as_number(x):
        try:
            return float(x)
        except ValueError:
            return x

    if number(a) and isinstance(b, str):
        return a, as_number(b)
    if number(b) and isinstance(a, str):
        return as_number(a), b
    return a, b


def demo():
    """Self-check: python -c 'import bpmn8n.core.variables as v; v.demo()'"""
    case = {"book": "Dune", "amount": 1200, "vip": False, "customer": {"email": "a@b.it"}, "hosts": ["h-1"]}
    assert render("{{amount}}", case) == 1200 and render("Book {{book}}!", case) == "Book Dune!"
    assert render("{{customer.email}} {{hosts.0}}", case) == "a@b.it h-1" and render("{{nope}}", case) is MISSING
    assert Condition.holds("{{amount}} > 1000 and not {{vip}}", case)
    assert Condition.holds('{{vip}} == false and {{book}} != "true"', case)
    assert Condition.holds("{{amount}} < 1000", {"amount": "450"}) and Condition.holds('{{zip}} == "00123"', {"zip": "00123"})
    assert Condition.holds("{{qty}} == 0 and {{qty}} != 1", {"qty": "0"})  # text from a form, compared as a number
    assert Condition.holds('{{s}} in ["a", "b"] and -1 < {{n}} <= 2', {"s": "a", "n": 2})
    for evil in ("__import__('os')", "{{book}}.upper()", "{{amount}} ** 99", "open", "_v0"):
        try:
            Condition.holds(evil, case)
            raise AssertionError(evil)
        except ConditionError:
            pass
