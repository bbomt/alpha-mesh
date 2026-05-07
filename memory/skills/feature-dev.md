# Skill: feature-dev

## What it does
Drives a complete Jira ticket → implementation → deploy → test → commit → update cycle
using Claude Code's `/feature-dev` plugin and the Atlassian MCP.

## When to use
Any time a Jira ticket has a clear acceptance criterion and can be implemented
in a single Claude Code session within one repo (or with cross-repo visibility).

## Invocation
```
/feature-dev:feature-dev Please use Atlassian to review Jira ticket <TICKET-ID> and implement the fix.
    When complete:
    1) deploy to dev (front end only if applicable)
    2) run all tests: pnpm test:all and verify all pass
    3) commit changes
    4) update JIRA
```

## Pre-conditions
- AWS SSO login active (`aws sso login`)
- Correct model active (`model status`)
- Task logged (`task-log start <TICKET-ID> '<title>' --repo <REPO>`)
- Claude Code opened in the correct repo (or root if cross-repo)

## Post-conditions
- `task-log done <TICKET-ID> --outcome '<what shipped>'`
- Run `dream` at end of session

## Known failure modes
- **Model unavailable mid-session**: run `model use minimax` and resume
- **Cross-repo changes**: open Claude Code at `~/environment/pmat/` root, not inside a specific repo
- **Deploy fails without SSO**: `aws sso login` then retry deploy step

## Notes
- The feature-dev plugin handles the multi-step loop autonomously; manual intervention
  is usually only needed when the ticket is underspecified
- Jira ticket quality directly affects output quality — investing in clear AC pays off
