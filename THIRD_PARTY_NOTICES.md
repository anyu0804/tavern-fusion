# Third-Party Notices & Architecture Attribution

**Tavern Fusion** is an independent Python 3.12 + JavaScript (ES Module) implementation released under the [MIT License](./LICENSE). It incorporates architectural patterns, persona conventions, and open data format specifications from the following upstream open-source projects:

---

## 1. `terva-sh/terva` (Architecture Inspiration & Specialist Persona Names)

- **Upstream Repository**: https://github.com/terva-sh/terva
- **License**: MIT License
- **Scope of Reference**:
  - Tavern Fusion retains and pays tribute to the five Finnish specialist persona names introduced by Terva (`Kertoja` Director, `Kartoittaja` Branch Mapper, `Seppä` Card Smith, `Toimittaja` State Folder, and `Dramaturgi` Lore Extractor).
  - Harness architecture concepts—including Stable Prefix / Ephemeral Tail context partitioning, `--chat` vs. `--play` mode separation, `Card-is-Data` zero-privilege boundary, `/media` magic-byte sandboxing, Card Doctor static linting, and interactive Stage revision workflows—are inspired by Terva's design documentation and independently implemented for Google Antigravity's plugin/sidecar runtime.

### Terva MIT License Notice

```text
MIT License

Copyright (c) Drew Short
Copyright (c) Patric Eckhart

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## 2. Character Card V2 / V3 Specification

- **Specification**: https://github.com/kwaroran/character-card-spec-v3 (`chara_card_v2` / `chara_card_v3`)
- **Scope of Use**: Standard PNG `tEXt` / `zTXt` / `iTXt` chunk encoding (`chara` and `ccv3`) implemented via Python's standard library (`struct`, `zlib`, `base64`, `json`).

---

## 3. SillyTavern & TavernAI Data Format Interoperability Notice

- **SillyTavern Upstream**: https://github.com/SillyTavern/SillyTavern (AGPL-3.0, maintained by Cohee, RossAscends, Wolfsblvt, and the SillyTavern Community; Docs: https://docs.sillytavern.app/)
- **TavernAI Progenitor**: https://github.com/TavernAI/TavernAI (maintained by Hum and contributors; original PNG `chara` tEXt chunk and tavern UI paradigm)
- **Clean-Room Boundary**: Tavern Fusion bundles **zero** SillyTavern or TavernAI source code. All prompt assembly (`OpenAI Settings/*.json`), World Info `selectiveLogic` and `@Depth` splicing, regex post-processing, and `.jsonl` transcript synchronization are independently written from scratch in Python 3.12 and vanilla ES Modules, interoperating strictly with user-owned local data files (`characters/*.png`, `worlds/*.json`, `chats/**/*.jsonl`, `OpenAI Settings/*.json`, `settings.json`) and optional public localhost HTTP endpoints (`127.0.0.1:8000`).

---

## 4. Antigravity IDE / Coding-Agent Tavern Pioneers & LINUX DO Community

- **Antigravity IDE × SillyTavern Bridge Pioneers**:
  - Inspired by `terva-sh/terva` (https://github.com/terva-sh/terva), the local first-generation `st-antigravity-mcp` (`sillytavern-bridge`) prototype, and community MCP bridges connecting AI coding IDEs with SillyTavern (such as `Chaunyzhang/sillytavern-mcp` and `Inversity/SillyTavern-MCP-Servers-Universal`).
- **LINUX DO Community (`https://linux.do`)**:
  - Special thanks to the **LINUX DO (L站)** community for pioneering and sharing best practices on SillyTavern prompt engineering ("Four Dimensional Locks": top-level `system_instruction` epistemic cutoff, `<START>` lexical dialogue convergence, World Info `@Depth` splicing, and regex immersion guards) as well as Google Antigravity IDE plugin/MCP workflows.
