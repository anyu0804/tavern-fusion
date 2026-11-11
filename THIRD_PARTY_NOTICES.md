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

- **SillyTavern Upstream**: https://github.com/SillyTavern/SillyTavern (AGPL-3.0, Docs: https://docs.sillytavern.app/)
- **TavernAI Progenitor**: https://github.com/TavernAI/TavernAI (original PNG `chara` tEXt chunk and tavern UI paradigm)
- **Clean-Room Boundary**: Tavern Fusion bundles **zero** SillyTavern or TavernAI source code. All prompt assembly (`OpenAI Settings/*.json`), World Info `selectiveLogic` and `@Depth` splicing, regex post-processing, and `.jsonl` transcript synchronization are independently written from scratch in Python 3.12 and vanilla ES Modules, interoperating strictly with user-owned local data files (`characters/*.png`, `worlds/*.json`, `chats/**/*.jsonl`, `OpenAI Settings/*.json`, `settings.json`) and optional public localhost HTTP endpoints (`127.0.0.1:8000`).

---

## 4. Declared Font Stack (SIL Open Font License 1.1)

- **Scope of Use**: The Tavern Stage sidecar stylesheets (`sidecars/tavern_stage/ui/styles.css`) and the browser extension (`st_web_extension/st-ag-sync`) **only declare** the following open-source font families and W3C generic family keywords (`system-ui`, `sans-serif`, `ui-monospace`, `monospace`). No font binary is bundled, embedded, or redistributed by this repository; the browser resolves the names against fonts installed on the local system and falls back to generic families when they are absent.
- **Declared families and their licenses**:

| Font family | Upstream project | License |
| :--- | :--- | :--- |
| Noto Sans SC / Noto Sans Mono CJK SC | [notofonts](https://github.com/notofonts) (Google) | SIL Open Font License 1.1 |
| Source Han Sans SC (思源黑体) | [adobe-fonts/source-han-sans](https://github.com/adobe-fonts/source-han-sans) | SIL Open Font License 1.1 |
| Inter | [rsms/inter](https://github.com/rsms/inter) | SIL Open Font License 1.1 |
| JetBrains Mono | [JetBrains/JetBrainsMono](https://github.com/JetBrains/JetBrainsMono) | SIL Open Font License 1.1 |
| Source Code Pro | [adobe-fonts/source-code-pro](https://github.com/adobe-fonts/source-code-pro) | SIL Open Font License 1.1 |

- **Commercial font exclusion**: The stylesheets deliberately exclude proprietary system fonts such as Microsoft YaHei (方正字库), PingFang SC (Apple), Segoe UI, and Consolas (Microsoft). This avoids any commercial redistribution or embedded-screenshot licensing risk associated with those font files.

---

## 5. Community Acknowledgments & Non-Affiliation Disclaimer

- **社区致谢**: 本项目认可并致谢 [LINUX DO 社区](https://linux.do/)（https://linux.do），感谢社区佬友在 AI Agent 工程实践与工具链上的交流分享与严谨审阅。
- **非官方声明**: 本项目为独立的非官方社区开源插件与工具集，与 Google LLC 及 SillyTavern 官方团队无任何隶属、授权或背书关系。文中提及的 Google、Antigravity、Gemini、SillyTavern、TavernAI 等名称仅用于说明兼容环境与数据格式，相关商标归各自所有者所有。
