# Git Workflow Rule

Always follow this Git workflow automatically without asking the user each time:

1. **Automatic Commits & Pushes**: Whenever a meaningful change is made (file added/changed, feature completed, bug fixed, config modified, etc.), automatically commit and push the changes.
2. **Commit Message Style**:
   - Must sound human-written, concise, terse, and natural.
   - Lowercase, direct, and developer-style.
   - Format: Include the change and brief reason on one line (e.g. `add retry logic for gemini api calls (was failing on rate limits)` or `fix: handle empty dataset edge case in scraper`).
   - NEVER use generic filler like "updated code", "made improvements", "minor changes", or formal AI phrasing.
3. **Approval Rules**:
   - Do NOT ask for permission for routine feature work, fixes, refactoring, or progress.
   - ONLY ask for permission before committing/pushing if:
     - It is a major/breaking change (architecture shift, core file purpose change, removing significant functionality, changing data flow).
     - The change significantly alters how the whole project works.
