# Publishing the pyrung VS Code Extension

## Prerequisites

- Install the VS Code Extension CLI:
  ```bash
  npm install -g @vscode/vsce
  ```

## Steps

### 1. Create a Publisher Account

1. Go to https://marketplace.visualstudio.com/manage
2. Sign in with a Microsoft account (or create one)
3. Create a publisher — the ID must match `"publisher"` in `package.json` (currently `"pyrung"`)

### 2. Create a Personal Access Token (PAT)

1. Go to https://dev.azure.com → User Settings → Personal Access Tokens
2. Create a new token:
   - **Organization**: All accessible organizations
   - **Scopes**: Marketplace → **Manage**
3. Save the token (not shown again)

### 3. Update `package.json`

Fields to add/verify before publishing:

- [ ] `"repository"` — add GitHub repo URL
- [ ] `"icon"` — 128x128 PNG icon (optional but recommended for discoverability)
- [ ] `"version"` — bump from `0.0.1` to `0.1.0` to match the pyrung release

### 4. Package and Publish

```bash
cd editors/vscode/pyrung-debug

# Login (once)
vsce login pyrung
# Paste PAT when prompted

# Package locally to verify
vsce package
# Creates pyrung-debug-<version>.vsix

# Test install
code --install-extension pyrung-debug-0.1.0.vsix

# Publish
vsce publish
```

### 5. Post-publish

- [ ] Update `docs/guides/dap-vscode.md` — replace "Pending publish" notice with install instructions
- [ ] Add marketplace badge to the extension README

## Optional: CI Publishing

Add a GitHub Actions workflow to auto-publish on tag:

```yaml
# .github/workflows/vscode-extension.yml
name: Publish VS Code Extension
on:
  push:
    tags: ['vscode-v*']
jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: 20 }
      - run: npm install -g @vscode/vsce
      - run: cd editors/vscode/pyrung-debug && vsce publish
        env:
          VSCE_PAT: ${{ secrets.VSCE_PAT }}
```

Store the PAT as a GitHub repository secret named `VSCE_PAT`.

## Open VSX (alternative)

To also publish to Open VSX (used by VSCodium, Gitpod):

```bash
npm install -g ovsx
ovsx publish pyrung-debug-0.1.0.vsix -p <open-vsx-token>
```

Register at https://open-vsx.org.
