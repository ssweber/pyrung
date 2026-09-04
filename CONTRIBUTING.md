# Contributing to pyrung

Bug reports and questions are the most useful thing you can send right now. A program that behaves differently in pyrung than on a CLICK PLC is a bug we want to know about.

## Report a bug

Open an [issue](https://github.com/ssweber/pyrung/issues/new/choose). The template asks for the smallest program that shows the problem, what you expected, and what happened. If it involves a CLICK PLC, say which CLICK Programming Software version and PLC model.

## Ask a question or share an idea

Use [Discussions](https://github.com/ssweber/pyrung/discussions). Ladder questions are welcome; you don't need to have a bug.

## Change the code

```bash
git clone https://github.com/ssweber/pyrung
cd pyrung
make install   # uv sync --all-extras --dev
make           # lint + test
```

Open a pull request against `main`. Keep it to one change. Use a [Conventional Commits](https://www.conventionalcommits.org/) title such as `fix(click): ...` or `docs: ...`, and add one line to `CHANGELOG.md` under Unreleased if the change is visible to users.

Text that pyrung prints must be ASCII. Docs may use Unicode.
