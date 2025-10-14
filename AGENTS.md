# AI Assistant Instructions

## Important Rules

1. **Always read README.md files** in each project directory to understand:

   - How to set up and run the project
   - Available commands and scripts
   - Code style and conventions
   - Testing requirements

2. **Before committing code changes**:

   - Run the formatting command specified in the project's README
   - Run linting and type checking as documented
   - Ensure all tests pass

## AI-Specific Behavior

### Code Writing

- Only use emojis if the user explicitly requests it
- Never proactively create documentation files (\*.md) unless explicitly requested
- Always prefer editing existing files over creating new ones

### Working with Files

- When editing a file, always read it first
- Never edit historical database migration files
- Place imports at the top of files (inline imports only to prevent circular dependencies)

### Pull Requests

- When updating pull requests with additional commits, update the PR description to include the latest changes
- Always run tests and formatting before creating a PR
