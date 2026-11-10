"""Antigravity `/generative_ui` Stage Widget Compiler (`widget_compiler.py`).

Compliant with Google Antigravity's `/generative_ui` CSP & theming specification:
- Uses the official allowlisted Tailwind script (`https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js`)
- Adapts automatically to light and dark host themes via CSS variables (`--background`, `--card`, `--foreground`, `--muted-foreground`, `--border`)
- Enforces `>= 12px` (`text-xs`) minimum font size floor
- Uses transparent body background in `inline` (`<= 500px`) mode
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .fs_safety import atomic_write_text


def compile_stage_widget_html(
    character_card: dict[str, Any],
    avatar_data_uri: str | None,
    messages: list[dict[str, Any]],
    variables: dict[str, Any],
    active_lore_trace: list[dict[str, Any]],
    dump_report: str,
    chat_file_name: str = "live_session.jsonl",
    placement: str = "standalone",
    mode: str = "play",
    output_html_path: str | Path | None = None,
) -> dict[str, Any]:
    data = character_card.get("data", {})
    char_name = str(data.get("name", "Character"))
    scenario_str = str(data.get("scenario", "Active Adventure Scene"))[:140]

    fallback_svg_uri = (
        "data:image/svg+xml;utf8,"
        "<svg xmlns='http://www.w3.org/2000/svg' width='256' height='256' viewBox='0 0 256 256'>"
        "<rect width='256' height='256' fill='%230f2922'/>"
        "<circle cx='128' cy='102' r='44' fill='%2334d399' fill-opacity='0.4'/>"
        "<path d='M56 220c14-44 130-44 144 0' fill='%2334d399' fill-opacity='0.4'/></svg>"
    )
    safe_avatar = avatar_data_uri or fallback_svg_uri
    is_inline = placement == "inline"
    body_bg_cls = "bg-transparent p-2" if is_inline else "bg-[var(--background)] p-4"

    state_payload = {
        "placement": "inline" if is_inline else "standalone",
        "mode": mode,
        "charName": char_name,
        "scenario": scenario_str,
        "chatFile": chat_file_name,
        "variables": variables or {"affection": 60},
        "loreTrace": active_lore_trace,
        "messages": messages[-12:],
        "dumpReport": dump_report,
    }
    state_json = json.dumps(state_payload, ensure_ascii=False).replace("</", "<\\/")

    html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Tavern Fusion Stage — {char_name}</title>
  <script src="https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js"></script>
  <style>
    .inline-cap {{ max-height: 484px; overflow: hidden; }}
    .thin-scroll::-webkit-scrollbar {{ width: 5px; height: 5px; }}
    .thin-scroll::-webkit-scrollbar-thumb {{ background: rgba(16,185,129,0.35); border-radius: 9999px; }}
  </style>
</head>
<body class="{body_bg_cls} text-[var(--foreground)] antialiased">
  <div id="stageRoot" class="mx-auto w-full rounded-xl bg-[var(--card)] border border-[var(--border)] shadow-sm overflow-hidden flex flex-col">
    <header class="px-4 py-3 border-b border-[var(--border)] flex flex-wrap items-center justify-between gap-2">
      <div class="flex items-center gap-3 min-w-0">
        <img id="avatarImg" src="{safe_avatar}" alt="{char_name}" class="w-11 h-11 rounded-full object-cover border-2 border-emerald-500/60 shrink-0" />
        <div class="min-w-0">
          <div class="flex items-center gap-2 flex-wrap">
            <h1 id="charTitle" class="text-sm font-semibold text-[var(--foreground)] truncate"></h1>
            <span class="text-xs px-2 py-0.5 rounded bg-emerald-500/15 text-emerald-600 dark:text-emerald-300 border border-emerald-500/30">CCv3</span>
            <span id="syncPill" class="text-xs px-2 py-0.5 rounded-full bg-[var(--secondary)] text-[var(--muted-foreground)]"></span>
          </div>
          <p id="scenarioLine" class="text-xs text-[var(--muted-foreground)] truncate"></p>
        </div>
      </div>
      <div class="flex items-center gap-1.5 text-xs">
        <button type="button" data-view="chat" class="tab-btn px-2.5 py-1 rounded-lg bg-emerald-600 text-white font-medium text-xs">对话流</button>
        <button type="button" data-view="lore" class="tab-btn px-2.5 py-1 rounded-lg bg-[var(--secondary)] text-[var(--foreground)] font-medium text-xs">世界书</button>
        <button type="button" data-view="dump" class="tab-btn px-2.5 py-1 rounded-lg bg-[var(--secondary)] text-[var(--foreground)] font-medium text-xs">Dump</button>
      </div>
    </header>

    <div id="varsRibbon" class="px-4 py-2 border-b border-[var(--border)] flex flex-wrap items-center justify-between gap-2 text-xs">
      <div id="varsItems" class="flex flex-wrap items-center gap-3"></div>
      <div id="lorePills" class="flex flex-wrap items-center gap-1.5"></div>
    </div>

    <section id="viewChat" class="p-4 space-y-3 overflow-y-auto thin-scroll max-h-[340px]"></section>
    <section id="viewLore" class="hidden p-4 space-y-2.5 overflow-y-auto thin-scroll max-h-[340px]"></section>
    <section id="viewDump" class="hidden p-4 overflow-y-auto thin-scroll max-h-[340px]">
      <pre id="dumpPre" class="text-xs font-mono text-[var(--foreground)] bg-[var(--background)] p-3 rounded-lg border border-[var(--border)] whitespace-pre-wrap"></pre>
    </section>
  </div>

  <script>
    const S = {state_json};

    function esc(t) {{
      return String(t || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }}

    function fmtRP(raw) {{
      const s = esc(raw);
      const act = s.replace(/\\*([^*]+)\\*/g, '<span class="italic text-emerald-600 dark:text-emerald-300 font-serif">$1</span>');
      return act.replace(/"([^"]+)"|“([^”]+)”/g, function (_, a, b) {{
        return '<span class="font-medium underline decoration-amber-500/40 underline-offset-2">“' + (a || b) + '”</span>';
      }});
    }}

    function render() {{
      const root = document.getElementById('stageRoot');
      if (S.placement === 'inline') {{
        root.classList.add('inline-cap');
      }} else {{
        root.classList.remove('inline-cap');
      }}

      document.getElementById('charTitle').textContent = S.charName;
      document.getElementById('syncPill').textContent = S.chatFile + ' · #' + S.messages.length + '楼';
      document.getElementById('scenarioLine').textContent = S.scenario;
      document.getElementById('dumpPre').textContent = S.dumpReport;

      const vEntries = Object.entries(S.variables || {{}});
      document.getElementById('varsItems').innerHTML = vEntries.map(function (kv) {{
        return '<span class="text-[var(--muted-foreground)]">' + esc(kv[0]) + ': <strong class="text-[var(--foreground)] font-mono">' + esc(kv[1]) + '</strong></span>';
      }}).join('');

      document.getElementById('lorePills').innerHTML = (S.loreTrace || []).map(function (lt) {{
        return '<button type="button" class="lore-jump text-xs px-2 py-0.5 rounded-full border border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300">'
          + esc(lt.comment) + ' · ' + esc(lt.position) + '</button>';
      }}).join('');

      document.querySelectorAll('.lore-jump').forEach(function (b) {{
        b.addEventListener('click', function () {{ setView('lore'); }});
      }});

      const msgs = S.placement === 'inline' ? S.messages.slice(-2) : S.messages;
      document.getElementById('viewChat').innerHTML = msgs.map(function (m, idx) {{
        const swipes = Array.isArray(m.swipes) && m.swipes.length > 0 ? m.swipes : [m.mes];
        const sId = Math.min(swipes.length - 1, Math.max(0, Number(m.swipe_id || 0)));
        const txt = swipes[sId] || m.mes;
        if (m.is_user) {{
          return '<div class="flex justify-end"><div class="max-w-[85%] rounded-2xl rounded-tr-sm bg-[var(--secondary)] border border-[var(--border)] px-3.5 py-2.5 text-xs">'
            + '<div class="text-xs font-semibold text-[var(--muted-foreground)] mb-1">' + esc(m.name) + '</div>' + fmtRP(txt) + '</div></div>';
        }}
        let swipeCtl = '';
        if (swipes.length > 1) {{
          swipeCtl = '<div class="mt-2 pt-1.5 border-t border-[var(--border)] flex justify-end gap-2 text-xs font-mono">'
            + '<button type="button" data-idx="' + idx + '" data-d="-1" class="sw-btn px-2 py-0.5 bg-[var(--secondary)] rounded">&lsaquo;</button>'
            + '<span>Swipe ' + (sId + 1) + '/' + swipes.length + '</span>'
            + '<button type="button" data-idx="' + idx + '" data-d="1" class="sw-btn px-2 py-0.5 bg-[var(--secondary)] rounded">&rsaquo;</button></div>';
        }}
        return '<div class="flex items-start gap-2.5"><img src="' + document.getElementById('avatarImg').src + '" class="w-8 h-8 rounded-full object-cover mt-0.5 shrink-0" />'
          + '<div class="flex-1 rounded-2xl rounded-tl-sm bg-[var(--background)] border border-[var(--border)] px-3.5 py-2.5 text-xs leading-relaxed">'
          + '<div class="text-xs font-semibold text-emerald-600 dark:text-emerald-300 mb-1">' + esc(m.name) + '</div>' + fmtRP(txt) + swipeCtl + '</div></div>';
      }}).join('');

      document.querySelectorAll('.sw-btn').forEach(function (btn) {{
        btn.addEventListener('click', function () {{
          const visibleOffset = S.placement === 'inline' ? Math.max(0, S.messages.length - 2) : 0;
          const mObj = S.messages[visibleOffset + Number(btn.getAttribute('data-idx'))];
          if (!mObj || !mObj.swipes) return;
          const d = Number(btn.getAttribute('data-d'));
          mObj.swipe_id = (Number(mObj.swipe_id || 0) + d + mObj.swipes.length) % mObj.swipes.length;
          render();
        }});
      }});

      document.getElementById('viewLore').innerHTML = (S.loreTrace || []).map(function (lt) {{
        return '<div class="p-3 rounded-lg bg-[var(--background)] border border-[var(--border)] text-xs space-y-1">'
          + '<div class="flex justify-between font-semibold"><span>#' + lt.uid + ' · ' + esc(lt.comment) + '</span>'
          + '<span class="font-mono text-xs text-[var(--muted-foreground)]">' + esc(lt.trigger) + ' | ' + esc(lt.position) + '</span></div>'
          + '<p class="text-[var(--muted-foreground)] text-xs leading-relaxed">' + esc(lt.content) + '</p></div>';
      }}).join('') || '<p class="text-xs text-[var(--muted-foreground)]">当前回合暂无激活的世界书词条。</p>';
    }}

    function setView(v) {{
      document.getElementById('viewChat').classList.toggle('hidden', v !== 'chat');
      document.getElementById('viewLore').classList.toggle('hidden', v !== 'lore');
      document.getElementById('viewDump').classList.toggle('hidden', v !== 'dump');
      document.querySelectorAll('.tab-btn').forEach(function (b) {{
        const active = b.getAttribute('data-view') === v;
        b.className = active
          ? 'tab-btn px-2.5 py-1 rounded-lg bg-emerald-600 text-white font-medium text-xs'
          : 'tab-btn px-2.5 py-1 rounded-lg bg-[var(--secondary)] text-[var(--foreground)] font-medium text-xs';
      }});
    }}

    document.querySelectorAll('.tab-btn').forEach(function (b) {{
      b.addEventListener('click', function () {{ setView(b.getAttribute('data-view')); }});
    }});

    render();
  </script>
</body>
</html>
"""

    saved_path_str: str | None = None
    if output_html_path is not None:
        out_p = atomic_write_text(output_html_path, html_doc)
        saved_path_str = str(out_p.resolve())

    return {
        "character_name": char_name,
        "placement": "inline" if is_inline else "standalone",
        "output_html_path": saved_path_str,
        "html_length": len(html_doc),
        "html": html_doc if output_html_path is None else f"Saved to {saved_path_str}",
    }
