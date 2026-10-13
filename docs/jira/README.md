# Jira backlog

`backlog.csv` holds 7 epics and 30 stories: 27 are Done and 3 are To Do.

## Import

1. In Jira, create a **Scrum** project with the key **`GLMG`**.
2. Go to **Settings (⚙) → System → External System Import → CSV**. On team-managed projects, use **Project → ⋯ → Import work items** instead.
3. Upload `backlog.csv` and choose the `GLMG` project.
4. Map the columns:
   - `Issue Id` → **Issue Id**
   - `Parent` → **Parent** (this links stories to their epics)
   - `Issue Type`, `Summary`, `Description`, `Status`, `Priority` and `Labels` → the fields with the same names
5. Run the import, then check that the stories sit under their epics.

## Link GitHub to Jira

1. Install **GitHub for Jira** from the Atlassian Marketplace (it's free) and connect the repo.
2. Put the ticket key in branch names and commits, for example `feature/GLMG-25-github-actions`, `GLMG-25: add CI workflow`.
3. Commits, PRs and CI status will then appear on each ticket.
