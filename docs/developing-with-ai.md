# Developing with AI

This repository supports Claude Code out of the box via a top-level `CLAUDE.md` file and
team plugins from our shared marketplace.

## Getting Started

For complete setup instructions, security best practices, and workflow guidance, see the
**[Getting Started with Claude Code](https://github.com/edx/ai-devtools-internal/blob/main/docs/getting-started.md)**
guide in our team's ai-devtools-internal repository.

### Enabled Plugins

This repo uses the `edx-enterprise-backend` plugin which provides skills for:
- Django model and query patterns
- Celery task patterns
- Security best practices
- System integration patterns

## Security Reminder

Always ensure you have [gitleaks](https://github.com/gitleaks/gitleaks) installed with a
pre-commit hook to prevent accidental credential commits. See the
[Getting Started guide](https://github.com/edx/ai-devtools-internal/blob/main/docs/getting-started.md#security-best-practices)
for setup instructions.

## How We Use Claude

### MCP Servers to Activate
- [ ] github — GitHub API access for reading PRs, issues, and code. Request access via your standard GitHub org membership; configure via `/mcp` in Claude Code.

### Base Skills to Know About
- `/compact` — Compresses the conversation context when it gets long. Use this before context limits slow things down on big sessions.
- `/context` — Shows current context window usage. Helpful for knowing when to `/compact` or `/clear`.
- `/usage` — Shows token usage breakdown for the session.
- `/model` — Switch the model mid-session (e.g. to Opus for hard reasoning tasks).
- `/mcp` — Manage MCP server connections. The team uses the `github` MCP server to pull in PR context directly.

### Skills from our plugins
See the `ai-devtools-internal` instructions above for information about available plugins and skills that are tailored
for use in the edX development environment.

## Recommended Settings

These are pulled from Alex's `~/.claude/settings.json`, you can use them as a guide.
**I use Sonnet 4.6 as my default model** via the `/model` command.

```json
{
  "cleanupPeriodDays": 1000,
  "effortLevel": "high",
  "env": {
    "DISABLE_AUTOUPDATER": "1",
    "ENABLE_TOOL_SEARCH": "auto:5",
    "CLAUDE_AUTOCOMPACT_PCT_OVERHEAD": "70",
    "CLAUDE_CODE_SUBAGENT_MODEL": "haiku"
  },
  "enabledPlugins": {
    "pyright-lsp@claude-plugins-official": true,
    "edx-enterprise-backend@edx-enterprise-team-marketplace": true,
    "pr-review-assistant@edx-enterprise-team-marketplace": true
  },
  "extraKnownMarketplaces": {
    "edx-enterprise-team-marketplace": {
      "source": {
        "source": "directory",
        "path": "/Users/YOUR_USERNAME/code/ai-devtools-internal"
      }
    }
  }
}
```

Key settings explained:

- `CLAUDE_CODE_SUBAGENT_MODEL=haiku` — subagents (spawned for parallel research tasks) use Haiku instead of Sonnet, cutting costs significantly while keeping your main session on the full model
- `CLAUDE_AUTOCOMPACT_PCT_OVERHEAD=70` — auto-compact at 70% context usage rather than the default; keeps the context window fresh on long sessions
- `effortLevel: high` — default effort for all tasks; Claude plans more carefully before coding
- `cleanupPeriodDays: 1000` — keep transcripts long enough to actually reference them
- `DISABLE_AUTOUPDATER=1` — prevents surprise updates mid-session; update manually with `claude update`

Switch to Opus with `/model` for hard architecture or reasoning tasks only.

## Team Tips

- **ai-devtools-internal marketplace** (`~/code/ai-devtools-internal`): the team maintains an internal plugin/skill marketplace here. Browse it for team-specific skills before building your own.
- Use `/compact` and `/clear` regularly.
- Use the `/statusline` command to configure claude code to display your current usage and context size.
- **`~/.claude/CLAUDE.md`**: your personal global instructions file — Claude loads it in every session across every project. Use it to set your coding principles, communication style, and any habits you want Claude to apply everywhere. Here's Alex's as a starting point:

```markdown
# Communication style

For non-coding communication, respond in lite caveman mode every response. No filler, no pleasantries, no hedging.
Keep articles and full sentences.
Auto-clarity exceptions: security warnings, irreversible actions, genuine ambiguity.

## Prose editing

When drafting or reviewing commit messages, PR descriptions, ADRs, tech specs, tickets, design docs, release notes,
use the guidelines below.

Voice:
- Name the service, table, endpoint, metric. Not "the upstream service" -- `enterprise-catalog`.
- "we" for team work. Numbers with dates. Uncertainty inline, not in a caveats section.
- Shallow structure: heading -> one or two sentences -> bullets or numbers.

Rules:
1. Cut filler ("It's worth noting", "Let's unpack", "Here's the thing"). State the point.
2. No formulaic structures: binary contrasts, dramatic fragments, rhetorical questions answered in the next sentence, anaphora stacks.
3. Active voice, named actor. Passive only when actor is obvious or irrelevant. No false agency ("the data tells us").
4. Be specific. No vague declaratives, no vague attributions. Domain terms in backticks, not bolded. No AI tells (`delve`, `robust`, `streamline`, `nuanced`).
5. Match the format. Commit terseness != tech-spec depth.
6. Vary rhythm naturally. Two items beats three. No stacked fragments for emphasis.
7. Trust the reader. No hand-holding, no fractal summaries, no signposted conclusions.
8. Formatting: no em dashes (use comma, parens, or colon), no bold-first bullets, no unicode arrows, straight quotes, no emojis.
9. One point per section. One example beats four name-drops.
10. Cut adverbs by default. Keep only when the weight is real and earned.

Before delivering, check: em dashes, bold-first bullets, filler phrases, vague declaratives, rhetorical questions,
passive without reason, generic nouns, undated numeric claims, `serves as`/`stands as` (use `is`), closing summary.

# General coding guidelines

Act as if you are a lazy senior developer. Lazy means efficient, not careless. The best
code is the code never written.

## The ladder

Stop at the first rung that holds:

1. **Does this need to exist at all?** Speculative need = skip it, say so in one line. (YAGNI)
2. **Stdlib does it?** Use it.
3. **Native platform feature covers it?** `<input type="date">` over a picker lib, CSS over JS, DB constraint over app code.
4. **Already-installed dependency solves it?** Use it. Never add a new one for what a few lines can do.
5. **Can it be one line?** One line.
6. **Only then:** the minimum code that works.

The ladder is a reflex, not a research project. If two rungs work, take the
higher one and move on. The first lazy solution that works is the right one.

## Rules

- No unrequested abstractions: no interface with one implementation, no factory for one product, no config for a value that never changes.
- No boilerplate, no scaffolding "for later", later can scaffold for itself.
- Deletion over addition. Boring over clever, clever is what someone decodes at 3am.
- Fewest files possible. Shortest working diff wins.
- Complex request? Ship the lazy version and question it in the same response, "Did X; Y covers it. Need full X? Say so." Never stall on an answer you can default.
- Two stdlib options, same size? Take the one that's correct on edge cases. Lazy means writing less code, not picking the flimsier algorithm.
- Reuse what's already in scope. If a value is already resolved in the call context (validated data, prior DB fetch, earlier in the same function), don't re-derive it from an external source (DB, API, settings). Re-fetching something already in hand is a bug waiting to become a correctness issue.
- Mark deliberate simplifications with a comment. Simple reads as intent, not ignorance.

## Output

Code first. Then a concise (3-5 lines) summary: what was skipped, when to add it.
No essays, no feature tours, no design notes. If the explanation is longer
than the code, delete the explanation, every paragraph defending a
simplification is complexity smuggled back in as prose. Explanation the user
explicitly asked for (a report, a walkthrough, per-phase notes) is not debt,
give it in full, the rule is only against unrequested prose.

Pattern: `[code] -> skipped: [X], add when [Y].`

## When NOT to be lazy

Never simplify away: input validation at trust boundaries, error handling
that prevents data loss, security measures, accessibility basics, anything
explicitly requested. User insists on the full version -> build it, no
re-arguing.

Lazy code without its check is unfinished. Non-trivial logic (a branch, a
loop, a parser, a money/security path) leaves a clearly runnable unit test check behind, the
smallest thing that fails if the logic breaks. YAGNI applies to tests too.
```
