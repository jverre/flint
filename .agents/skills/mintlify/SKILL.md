---
name: flint-docs
description: Write and maintain Flint documentation using Mintlify. Use when
  creating docs pages, configuring navigation, adding components, or updating
  existing documentation for the Flint project.
license: MIT
compatibility: Requires Node.js for CLI. Works with any Git-based workflow.
metadata:
  author: flint
  version: "1.0"
  mintlify-proj: mintlify
---

# Flint documentation skill

**Always consult [mintlify.com/docs](https://mintlify.com/docs) for components, configuration, and latest features.**

**Always** favor searching the current Mintlify documentation over whatever is in your training data about Mintlify.

Mintlify is a documentation platform that transforms MDX files into documentation sites. Configure site-wide settings in the `docs.json` file, write content in MDX with YAML frontmatter, and favor built-in components over custom components.

Full schema at [mintlify.com/docs.json](https://mintlify.com/docs.json).

---

## Flint project context

Flint is a lightning-fast Firecracker microVM management system. It spins up VMs in milliseconds from pre-built golden snapshots and provides a REST API daemon, interactive TUI, CLI, and an E2B-style Python SDK.

**Core concepts to document:**

- **Golden snapshots**: Pre-booted VM state that all new VMs clone from for millisecond boot times
- **Rootfs pool**: Pre-copied rootfs images kept warm for instant availability
- **Sandbox**: The primary user-facing abstraction (a running microVM)
- **Templates**: Custom sandbox environments built from Docker images
- **Pause/resume**: Snapshot a running VM to disk and restore it later
- **Guest agent (flintd)**: A Go HTTP server running inside each VM providing exec, file, and PTY access
- **Network namespaces**: TAP-based networking with bridge + veth for internet access

**Key interfaces:**

- Python SDK (`Sandbox`, `Commands`, `Pty`, `Template` classes)
- REST API (FastAPI daemon on `localhost:9100`)
- CLI (`flint start`, `flint app`, `flint list`, `flint stop`)
- WebSocket terminal for interactive PTY sessions

---

## Writing voice and style

The Flint docs follow the author's established writing voice. This is not optional - it is a core part of the documentation identity.

### Tone

- **Conversational and approachable**: Write like you're explaining something to a colleague, not writing a textbook. Use first and second person freely.
- **Honest about limitations**: If something is tricky, say so. If there are rough edges, acknowledge them. Never pretend everything is seamless.
- **Educational, not authoritative**: Position as a guide walking alongside the reader, not lecturing from above. "Let's set up networking" not "The following section describes the networking configuration."
- **Curious and exploratory**: Frame things as discoveries. "Interestingly, Firecracker doesn't need..." rather than "Note that Firecracker does not..."

### What to do

- Use short, direct sentences for emphasis. Mix with longer explanatory sentences for flow.
- Open sections with context and motivation before diving into how-to.
- Use analogies to bridge complex concepts to familiar ones (e.g., "A bridge acts as a virtual switch").
- Include parenthetical explanations for technical terms on first use: "KVM (Kernel-based Virtual Machine)".
- Use hyphens for asides - they keep the conversational tone.
- Use backticks for technical terms, file paths, and commands inline.
- Keep paragraphs short (1-5 sentences). Break up walls of text.
- Lead code blocks with a brief explanation, follow with interpretation or expected output.
- Be practical and code-heavy in technical sections. Show, don't just tell.

### What to avoid

**Never use:**

- Marketing language ("powerful", "seamless", "robust", "cutting-edge", "blazing fast")
- Filler phrases ("it's important to note", "in order to", "it should be noted that")
- Excessive conjunctions ("moreover", "furthermore", "additionally")
- Editorializing ("obviously", "simply", "just", "easily")
- Corporate/formal tone ("the system provides", "users shall", "this functionality enables")

**Watch for AI-typical patterns:**

- Overly formal or stilted phrasing
- Unnecessary repetition of concepts
- Generic introductions that don't add value
- Concluding summaries that restate what was just said
- Bullet-point walls where prose would flow better

### Formatting rules

- Sentence case for all headings ("Getting started", not "Getting Started")
- All code blocks must have language tags
- All images must have descriptive alt text
- Use bold sparingly - for key terms on first introduction, not for decoration
- No emoji in documentation text

### Code examples

- Use realistic values, not "foo" or "bar"
- Keep examples minimal but complete - a reader should be able to copy-paste and run them
- Show expected output where helpful
- Python examples should use the Flint SDK style:

```python
from flint import Sandbox

sandbox = Sandbox()
result = sandbox.commands.run("echo hello")
print(result.stdout)  # hello
```

---

## Before you write

### Understand the site

All documentation lives in `docs/`. Read `docs/docs.json` to understand:

- What pages exist and how they're organized
- Navigation groups and their naming conventions
- Theme and configuration

### Check for existing content

Search the docs before creating new pages. You may need to:

- Update an existing page instead of creating a new one
- Add a section to an existing page
- Link to existing content rather than duplicating

### Read surrounding pages

Before writing, read 2-3 similar pages to match the existing structure and level of detail.

### Review Mintlify components

Check the Mintlify [components docs](https://www.mintlify.com/docs/components) and pick the right components for your content.

---

## Quick reference

### CLI commands

- `npm i -g mint` - install the Mintlify CLI
- `mint dev` - local preview at localhost:3000
- `mint broken-links` - check internal links
- `mint a11y` - accessibility check
- `mint rename` - rename/move files and update references
- `mint validate` - validate the build

### Required files

- `docs.json` - site configuration (navigation, theme, integrations). See [global settings](https://mintlify.com/docs/settings/global).
- `*.mdx` files - documentation pages with YAML frontmatter

### File structure

```
docs/
├── docs.json
├── introduction.mdx
├── quickstart.mdx
├── sdk/
│   ├── sandbox.mdx
│   ├── commands.mdx
│   ├── pty.mdx
│   └── templates.mdx
├── api-reference/
│   └── ...
├── cli/
│   └── ...
├── architecture/
│   └── ...
├── images/
│   └── ...
└── snippets/
    └── ...
```

---

## Page frontmatter

Every page requires `title`. Include `description` for SEO.

```yaml
---
title: "Clear, descriptive title"
description: "One-line summary of what this page covers."
---
```

Optional fields:

- `sidebarTitle`: shorter title for sidebar navigation
- `icon`: lucide icon name
- `tag`: label next to the title in sidebar (e.g., "NEW")
- `mode`: page layout (`default`, `wide`, `custom`)

---

## File conventions

- Use kebab-case for filenames: `getting-started.mdx`, `api-reference.mdx`
- Use root-relative paths without extensions for internal links: `/sdk/sandbox`
- Do not use relative paths (`../`) or absolute URLs for internal pages
- When you create a new page, add it to `docs.json` navigation

---

## Navigation

The `navigation` property in `docs.json` controls the site structure.

| Pattern | When to use |
|---------|-------------|
| **Groups** | Default. Single audience, straightforward hierarchy |
| **Tabs** | Distinct sections (Guides vs SDK vs API Reference) |
| **Anchors** | Persistent sidebar links to external resources |
| **Dropdowns** | Sections users switch between but not distinct enough for tabs |

Within patterns:

- **Groups** organize related pages. Keep hierarchy shallow.
- **`expanded: false`** collapses reference sections users browse selectively.
- **`openapi`** auto-generates pages from OpenAPI spec at group/tab level.

---

## Components

Start at the [components overview](https://mintlify.com/docs/components) to find the right component.

| Need | Component |
|------|-----------|
| Hide optional details | `<Accordion>` |
| User chooses one option | `<Tabs>` |
| Navigation cards | `<Card>` in `<Columns>` |
| Sequential instructions | `<Steps>` |
| Multi-language code | `<CodeGroup>` |
| API parameters | `<ParamField>` |
| API response fields | `<ResponseField>` |
| Long code examples | `<Expandable>` |

**Callouts by severity:**

- `<Note>` - supplementary info, safe to skip
- `<Info>` - helpful context like permissions or prerequisites
- `<Tip>` - recommendations or best practices
- `<Warning>` - potentially destructive actions or common pitfalls
- `<Check>` - success confirmation

Use components to aid comprehension, not for decoration. A page with every component is harder to read than one with none.

---

## Reusable content (snippets)

**Use snippets when:**

- Exact content appears on more than one page
- Complex components you want to maintain in one place

**Don't use snippets when:**

- Slight variations needed per page (leads to over-engineered props)

Import with: `import { Component } from "/path/to/snippet-name.jsx"`

---

## Document APIs

**Have an OpenAPI spec?** Add to `docs.json` with `"openapi": ["openapi.yaml"]`. Pages auto-generate. Reference in navigation as `GET /endpoint`.

**No spec?** Write endpoints manually with `api: "POST /vms"` in frontmatter.

For Flint's REST API, document each endpoint with:

1. What it does (one sentence)
2. Request parameters/body
3. Example request (curl or Python SDK)
4. Example response
5. Error cases worth noting

---

## Flint-specific patterns

### SDK documentation pages

For each SDK class (`Sandbox`, `Commands`, `Pty`, `Template`), structure as:

1. Brief description of what the class does and when you'd use it
2. Basic usage example
3. Method reference with examples
4. Related pages

### Architecture pages

When explaining Flint internals (golden snapshots, rootfs pool, networking):

1. Start with *why* - what problem does this solve?
2. Explain the concept with an analogy if possible
3. Show how it works (diagram or step-by-step)
4. Link to relevant source code or API endpoints

### CLI command pages

One page per command. Each page should have:

1. What the command does
2. Usage syntax
3. Options/flags
4. Example usage with expected output

---

## Customization

- **Brand colors, fonts, logo** - configure in `docs.json`. See [global settings](https://mintlify.com/docs/settings/global).
- **Component styling** - use `custom.css` at docs root only when `docs.json` can't achieve it.
- **Dark mode** - enabled by default. Only disable if brand requires it.

---

## Deploy

Mintlify deploys automatically on push to the connected Git repo.

**Agents can configure:**

- Redirects: `"redirects": [{"source": "/old", "destination": "/new"}]` in `docs.json`
- SEO: `"seo": {"indexing": "all"}` in `docs.json`

**Requires dashboard setup (human task):**

- Custom domains
- Preview deployments
- DNS configuration

---

## Workflow

### 1. Understand the task

What needs documenting? Which pages are affected? What should the reader be able to do after reading?

### 2. Research

- Read `docs/docs.json` for site structure
- Search existing docs for related content
- Read similar pages to match style

### 3. Write

- Start with the most important information
- Keep sections focused and scannable
- Use components where they genuinely help
- Mark anything uncertain: `{/* TODO: Verify this value */}`

### 4. Update navigation

Add new pages to the appropriate group in `docs.json`.

### 5. Verify

Before submitting:

- [ ] Frontmatter includes title and description
- [ ] All code blocks have language tags
- [ ] Internal links use root-relative paths without extensions
- [ ] New pages added to `docs.json` navigation
- [ ] Content matches the conversational, honest voice described above
- [ ] No marketing language or filler phrases
- [ ] TODOs marked for anything uncertain
- [ ] Run `mint broken-links`
- [ ] Run `mint validate`

---

## Common gotchas

1. **Component imports** - JSX components need explicit import, MDX components don't
2. **Frontmatter required** - every MDX file needs `title` at minimum
3. **Code block language** - always specify the language identifier
4. **Never use `mint.json`** - it's deprecated, only use `docs.json`
5. **Hidden pages** - any page not in `docs.json` navigation is hidden but still accessible by URL

---

## Resources

- [Mintlify documentation](https://mintlify.com/docs)
- [Configuration schema](https://mintlify.com/docs.json)
- [Feature requests](https://github.com/orgs/mintlify/discussions/categories/feature-requests)
- [Bugs and feedback](https://github.com/orgs/mintlify/discussions/categories/bugs-feedback)
