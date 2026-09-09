"""Small, fail-closed TypeScript syntax parser for dependency fingerprints.

This is a supported grammar, not a TypeScript compiler. Unsupported syntax raises a coverage
error; no repository code is imported. Source positions live outside the fingerprint AST.
"""
import re

LEX = re.compile(r"(?:[A-Za-z_$][\w$]*|(?:\d+(?:\.\d+)?(?:[eE][+-]?\d+)?n?)|===|!==|=>|==|!=|<=|>=|\?\.|\?\?|&&|\|\||\*\*|\+\+|--|\+=|-=|\*=|/=|\.\.\.|[{}()[\],;:.?~!+*/%<>=&|^\-])")
BINARY = {"=": 1, "+=": 1, "-=": 1, "*=": 1, "/=": 1, "??": 3, "||": 3, "&&": 4,
          "|": 5, "^": 6, "&": 7, "==": 8, "!=": 8, "===": 8, "!==": 8,
          "<": 9, ">": 9, "<=": 9, ">=": 9, "in": 9, "instanceof": 9,
          "+": 10, "-": 10, "*": 11, "/": 11, "%": 11, "**": 12}


def lex(text):
    tokens, i, line = [], 0, 1
    while i < len(text):
        ch = text[i]
        if ch.isspace():
            line += ch == "\n"
            i += 1
            continue
        if text.startswith("//", i):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise ValueError("unterminated TypeScript comment")
            line += text[i:end + 2].count("\n")
            i = end + 2
            continue
        start_line = line
        if ch in "\"'`":
            quote, value, i = ch, "", i + 1
            while i < len(text) and text[i] != quote:
                if text.startswith("${", i) and quote == "`":
                    raise ValueError("interpolated templates are outside the supported TypeScript grammar")
                if text[i] == "\\":
                    i += 1
                    if i >= len(text):
                        raise ValueError("unterminated string escape")
                    escape = text[i]
                    if escape in "\r\n\u2028\u2029":
                        # JavaScript removes escaped physical line terminators from the value.
                        if escape == "\r" and i + 1 < len(text) and text[i + 1] == "\n":
                            i += 1
                        line += 1
                        i += 1
                        continue
                    if escape in "uUx":
                        raise ValueError("hex/unicode string escapes are outside the supported TypeScript grammar")
                    value += {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}.get(escape, escape)
                else:
                    if text[i] in "\r\n" and quote != "`":
                        raise ValueError("newline in TypeScript string")
                    value += text[i]
                line += text[i] == "\n"
                i += 1
            if i >= len(text):
                raise ValueError("unterminated TypeScript string")
            i += 1
            tokens.append(("template" if quote == "`" else "string", value, start_line, line))
            continue
        match = LEX.match(text, i)
        if not match:
            raise ValueError(f"unsupported TypeScript token at line {line}")
        value = match.group()
        kind = "number" if value[0].isdigit() else "word" if re.fullmatch(r"[A-Za-z_$][\w$]*", value) else "punct"
        tokens.append((kind, value, line, line))
        i = match.end()
    tokens.append(("eof", "<eof>", line, line))
    return tokens


class Parser:
    def __init__(self, text):
        self.tokens, self.i, self.declarations, self.scope = lex(text), 0, {}, []

    def peek(self, offset=0):
        return self.tokens[min(self.i + offset, len(self.tokens) - 1)][1]

    def take(self, value=None):
        token = self.tokens[self.i]
        if value is not None and token[1] != value:
            raise ValueError(f"expected {value!r} at TypeScript line {token[2]}")
        self.i += 1
        return token

    def eat(self, value):
        if self.peek() == value:
            self.take()
            return True
        return False

    def name(self):
        token = self.take()
        if token[0] != "word":
            raise ValueError(f"expected TypeScript declaration name at line {token[2]}")
        return token[1]

    def register(self, name, node, line):
        key = ".".join(self.scope + [name])
        self.declarations.setdefault(key, []).append((node, line))
        return node

    def end(self):
        if self.eat(";") or self.peek() in ("}", "<eof>"):
            return
        if self.tokens[self.i][2] > self.tokens[self.i - 1][3]:
            return
        raise ValueError(f"unsupported/missing statement boundary at line {self.tokens[self.i][2]}")

    def type_tokens(self, stops, declarations=False):
        values, stack = [], []
        pairs = {"(": ")", "[": "]", "{": "}", "<": ">"}
        while self.peek() != "<eof>":
            value = self.peek()
            if not stack and value in stops and (values or value != "{"):
                break
            if declarations and values and not stack and self.tokens[self.i][2] > self.tokens[self.i - 1][3] and value in {"export", "const", "let", "var", "function", "type", "interface", "class", "import", "declare"}:
                break
            token = self.take()
            if value in pairs:
                stack.append(pairs[value])
            elif value in (")", "]", "}", ">"):
                if not stack or stack.pop() != value:
                    raise ValueError("unbalanced TypeScript type")
            # Semicolons and trailing commas delimit members/types, not their meaning.
            if value == ";" or (value == "," and self.peek() in (")", "]", "}", ">")):
                continue
            values.append([token[0], token[1]])
        if stack or not values:
            raise ValueError("missing or incomplete TypeScript type")
        return values

    def params(self):
        self.take("(")
        rows = []
        while self.peek() != ")":
            spread = self.eat("...")
            name = self.name()
            optional = self.eat("?")
            annotation = self.type_tokens({",", ")", "="}) if self.eat(":") else None
            default = self.expr() if self.eat("=") else None
            rows.append([name, spread, optional, annotation, default])
            if not self.eat(","):
                break
        self.take(")")
        return rows

    def block(self):
        self.take("{")
        rows = []
        while self.peek() != "}":
            if self.peek() == "<eof>":
                raise ValueError("unterminated TypeScript block")
            rows.append(self.statement())
        self.take("}")
        return rows

    def function(self, flags, expression=False):
        line = self.take("function")[2]
        generator = self.eat("*")
        name = self.name() if self.peek() != "(" else ""
        generics = self.type_tokens({"("}) if self.peek() == "<" else None
        params = self.params()
        annotation = self.type_tokens({"{"}) if self.eat(":") else None
        self.scope.append(name or "<anonymous>")
        body = self.block()
        self.scope.pop()
        node = ["function", flags, name, generator, generics, params, annotation, body]
        if name:
            self.register(name, node, line)
        elif not expression:
            raise ValueError("anonymous function declaration is unsupported")
        return node

    def statement(self):
        start, flags = self.tokens[self.i][2], []
        while self.peek() in ("export", "default", "declare", "async"):
            if self.peek() == "async" and self.tokens[self.i + 1][2] > self.tokens[self.i][3]:
                break
            flags.append(self.take()[1])
        value = self.peek()
        if value == "function":
            return self.function(flags)
        if value in ("const", "let", "var"):
            kind, rows = self.take()[1], []
            while True:
                name = self.name()
                annotation = self.type_tokens({"=", ",", ";"}, declarations=True) if self.eat(":") else None
                expression = self.expr() if self.eat("=") else None
                node = [kind, flags, name, annotation, expression]
                self.register(name, node, start)
                rows.append(node)
                if not self.eat(","):
                    break
            self.end()
            return ["variables", rows]
        if value in ("interface", "type"):
            kind, name = self.take()[1], self.name()
            if kind == "interface":
                prefix = self.type_tokens({"{"}) if self.peek() != "{" else []
                self.take("{")
                body = self.type_tokens({"}"}) if self.peek() != "}" else []
                self.take("}")
                self.eat(";")
            else:
                prefix = self.type_tokens({"="}) if self.peek() != "=" else []
                self.take("=")
                # Type aliases require a semicolon unless they are the last declaration.
                body = self.type_tokens({";", "<eof>"}, declarations=True)
                self.eat(";")
            return self.register(name, [kind, flags, name, prefix, body], start)
        if value == "class":
            self.take()
            name = self.name()
            prefix = self.type_tokens({"{"}) if self.peek() != "{" else []
            self.take("{")
            members = []
            self.scope.append(name)
            while self.peek() != "}":
                line, modifiers = self.tokens[self.i][2], []
                while self.peek() in ("public", "private", "protected", "static", "readonly", "async", "get", "set", "abstract", "override"):
                    modifiers.append(self.take()[1])
                member = self.name()
                optional = self.eat("?")
                if self.peek() == "(":
                    params = self.params()
                    annotation = self.type_tokens({"{"}) if self.eat(":") else None
                    node = ["method", modifiers, member, optional, params, annotation, self.block()]
                else:
                    annotation = self.type_tokens({"=", ";", "}"}) if self.eat(":") else None
                    initial = self.expr() if self.eat("=") else None
                    self.end()
                    node = ["property", modifiers, member, optional, annotation, initial]
                members.append(self.register(member, node, line))
            self.scope.pop()
            self.take("}")
            return self.register(name, ["class", flags, name, prefix, members], start)
        if value == "import":
            self.take()
            parts, names = [], []
            while self.tokens[self.i][0] != "string":
                token = self.take()
                if token[0] == "eof" or token[1] in ("(", "=", ";"):
                    raise ValueError("unsupported TypeScript import declaration")
                if token[1] == "," and self.peek() == "}":
                    continue
                parts.append([token[0], token[1]])
                if token[0] == "word" and token[1] not in ("type", "from", "as"):
                    names.append(token[1])
            module = self.take()[1]
            attributes = self.expr() if self.eat("with") else None
            self.end()
            node = ["import", parts, module, attributes]
            for name in set(names):
                self.register("import:" + name, node, start)
            return node
        if flags:
            raise ValueError("unsupported TypeScript declaration modifiers")
        if value == "{":
            return ["block", self.block()]
        if value == ";":
            self.take()
            return ["empty"]
        if value in ("return", "throw", "break", "continue"):
            token = self.take()
            expression = None
            if self.peek() not in (";", "}", "<eof>") and self.tokens[self.i][2] == token[3]:
                expression = self.expr()
            self.end()
            return [token[1], expression]
        if value in ("if", "while"):
            self.take()
            self.take("(")
            test = self.expr()
            self.take(")")
            body = self.statement()
            alternate = self.statement() if value == "if" and self.eat("else") else None
            return [value, test, body, alternate]
        if value in ("for", "switch", "try", "do", "enum", "namespace"):
            raise ValueError(f"{value} is outside the supported TypeScript grammar")
        expression = self.expr()
        self.end()
        return ["expression", expression]

    def expr(self, minimum=0):
        token = self.take()
        value = token[1]
        if value == "async" and self.tokens[self.i][2] == token[3] and self.peek() not in (";", "}", "<eof>"):
            begin, end, depth = self.i, self.i, 0
            if self.peek() == "(":
                while end < len(self.tokens) - 1:
                    depth += (self.tokens[end][1] == "(") - (self.tokens[end][1] == ")")
                    end += 1
                    if depth == 0:
                        break
            else:
                end += 1
            if self.tokens[min(end, len(self.tokens) - 1)][1] in ("=>", ":"):
                return ["async", self.expr(0)]
        if value == "function":
            self.i -= 1
            left = self.function([], expression=True)
        elif value in ("!", "~", "+", "-", "typeof", "void", "delete", "await", "new", "++", "--"):
            left = ["unary", value, self.expr(13)]
        elif value == "(":
            begin = self.i - 1
            # Parenthesized arrow parameters are parsed as declarations, not expressions.
            depth, end = 1, self.i
            while depth and self.tokens[end][0] != "eof":
                depth += (self.tokens[end][1] == "(") - (self.tokens[end][1] == ")")
                end += 1
            following = self.tokens[end][1]
            if following in ("=>", ":"):
                self.i = begin
                params = self.params()
                annotation = self.type_tokens({"=>"}) if self.eat(":") else None
                self.take("=>")
                return ["arrow", params, annotation, ["block", self.block()] if self.peek() == "{" else self.expr()]
            left = ["parenthesized", self.expr()]
            self.take(")")
        elif value in ("{", "["):
            close, rows = ("}" if value == "{" else "]"), []
            while self.peek() != close:
                if value == "[" and self.eat(","):
                    rows.append(["hole"])
                    continue
                if self.eat("..."):
                    rows.append(["spread", self.expr()])
                elif value == "{":
                    key = self.take()
                    if key[0] not in ("word", "string", "number"):
                        raise ValueError("computed/object methods are outside the supported TypeScript grammar")
                    assigned = self.eat(":")
                    item = self.expr() if assigned else ["identifier", key[1]]
                    rows.append(["property" if assigned else "shorthand", [key[0], key[1]], item])
                else:
                    rows.append(self.expr())
                if not self.eat(","):
                    break
            self.take(close)
            left = ["object" if value == "{" else "array", rows]
        elif token[0] in ("string", "number", "template"):
            left = [token[0], value]
        elif token[0] == "word":
            left = ["identifier", value]
            if self.eat("=>"):
                return ["arrow", [[value, False, False, None, None]], None, ["block", self.block()] if self.peek() == "{" else self.expr()]
        else:
            raise ValueError(f"unsupported TypeScript expression at line {token[2]}: {value}")
        while True:
            value = self.peek()
            if value in (".", "?."):
                self.take()
                if self.peek() not in ("(", "["):
                    left = ["member", value, left, self.name()]
                    continue
                left = ["optional", left]
                value = self.peek()
            if value == "<":
                cursor, depth = self.i, 0
                while cursor < len(self.tokens) - 1:
                    item = self.tokens[cursor][1]
                    depth += (item == "<") - (item == ">")
                    cursor += 1
                    if depth == 0 or item in (";", "{", "}"):
                        break
                if depth == 0 and self.tokens[cursor][1] == "(":
                    left = ["type_arguments", left, self.type_tokens({"("})]
                    value = self.peek()
            if value == "(":
                self.take()
                arguments = []
                while self.peek() != ")":
                    arguments.append(["spread", self.expr()] if self.eat("...") else self.expr())
                    if not self.eat(","):
                        break
                self.take(")")
                left = ["call", left, arguments]
            elif value == "[":
                self.take()
                index = self.expr()
                self.take("]")
                left = ["index", left, index]
            elif value in ("++", "--") and self.tokens[self.i][2] == self.tokens[self.i - 1][3]:
                left = ["postfix", self.take()[1], left]
            elif value in ("as", "satisfies"):
                operation = self.take()[1]
                left = [operation, left, self.type_tokens({",", ";", ")", "]", "}", "?"})]
            elif value == "?" and minimum <= 2:
                self.take()
                yes = self.expr()
                self.take(":")
                left = ["conditional", left, yes, self.expr(2)]
            elif value in BINARY and BINARY[value] >= minimum:
                self.take()
                precedence = BINARY[value]
                left = ["binary", value, left, self.expr(precedence if value in ("=", "**") else precedence + 1)]
            else:
                break
        return left

    def parse(self):
        rows = []
        while self.peek() != "<eof>":
            rows.append(self.statement())
        return ["source", rows]


def fingerprint_tree(text, selector=""):
    parser = Parser(text)
    tree = parser.parse()
    if not selector:
        return tree, 1
    matches = parser.declarations.get(selector, [])
    if len(matches) != 1:
        raise ValueError("selected TypeScript declaration is missing or ambiguous")
    return matches[0]
