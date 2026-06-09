---
description: Review the entire repository, not just a diff
allowed-tools: Bash(git ls-files:*), Bash(rg:*), Read, Grep, Glob
argument-hint: [optional path or focus area]
---

Perform a comprehensive code review of the ENTIRE repository — not a git diff.

Scope: $ARGUMENTS
(If no scope is given, review the whole codebase (except files outlined in .gitignore and peronsal data files))

Files in the repo:
!`git ls-files`

Steps:
1. Map the structure and identify the main modules/entry points.
2. Review for bugs, security issues, error handling, and architecture concerns.
3. Check adherence to any CLAUDE.md conventions in the repo.
4. Group findings by severity and file, with specific line references.