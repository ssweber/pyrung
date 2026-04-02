# VS Code Extension — Publishing Plan

## Current approach: GitHub Release artifact

The `.vsix` is distributed as a GitHub release asset. Users download and install manually:

```bash
code --install-extension pyrung-debug-0.1.0.vsix
```

## Building the .vsix

```bash
npm install -g @vscode/vsce
cd editors/vscode/pyrung-debug
vsce package
```

## Future: Marketplace publishing

When the extension stabilizes, publish to the VS Code Marketplace:

1. Create a publisher account at https://marketplace.visualstudio.com/manage (publisher ID: `ssweber`)
2. Create a Personal Access Token at https://dev.azure.com → User Settings → Personal Access Tokens (Organization: All accessible, Scopes: Marketplace → Manage)
3. `vsce login ssweber` and paste the PAT
4. `vsce publish`

### Optional: CI publishing

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

### Open VSX (alternative)

```bash
npm install -g ovsx
ovsx publish pyrung-debug-0.1.0.vsix -p <open-vsx-token>
```

Register at https://open-vsx.org.
