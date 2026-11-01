"""Repo context navigator (#115): hierarchical decision-model retrieval for worker context (DecisionRAG).

Builds a lightweight hierarchical index of the repository tree (folders -> files -> chunks) with short
descriptions, navigates top-down within a per-level budget (`top_folders`, `top_files`, `top_chunks`),
applies a lexical/grep fallback so relevant files inside pruned folders are still recovered, formats the
selected context for worker prompts and handoff notes, and computes `recall` against the files a job
actually touched.
"""
from pathlib import Path
import re

from . import security

SKIP_DIRS = {
    '.git', '__pycache__', '.venv', 'venv', 'node_modules', 'dist', 'build',
    '.pytest_cache', '.mypy_cache', '.tox', 'Pods', 'DerivedData',
}
TEXT_SUFFIXES = {'.py', '.md', '.json', '.toml', '.yml', '.yaml', '.sh', '.swift', '.js', '.ts', '.tsx'}
TOKEN_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_.-]{1,40}')


def _tokens(text):
    return {t.lower() for t in TOKEN_RE.findall(text or '') if len(t) >= 2}


def _describe_file(path, text):
    first_lines = [line.strip().strip('"\'# ') for line in (text or '').splitlines()[:10] if line.strip()]
    summary = first_lines[0][:160] if first_lines else path.name
    defs = re.findall(r'^\s*(?:def|class|struct|enum|func)\s+([A-Za-z_][A-Za-z0-9_]*)', text or '', re.M)
    if defs:
        summary += ' (' + ', '.join(defs[:6]) + ')'
    return f'{path.as_posix()}: {summary}'


def _split_chunks(rel_path, text, chunk_lines=40):
    lines = (text or '').splitlines()
    if not lines:
        return []
    chunks = []
    for start in range(0, len(lines), chunk_lines):
        block = '\n'.join(lines[start:start + chunk_lines])
        chunks.append({
            'file': rel_path,
            'start_line': start + 1,
            'end_line': min(len(lines), start + chunk_lines),
            'text': security.redact(block[:1200]),
        })
    return chunks


def build_hierarchy(repo_root, max_file_bytes=200_000):
    """Build an offline hierarchy `{'folders': {...}, 'files': {...}, 'chunks': {...}}` for `repo_root`."""
    root = Path(repo_root).expanduser().resolve()
    folders = {}
    files = {}
    chunks = {}
    for path in sorted(root.rglob('*')):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if not path.is_file() or path.is_symlink() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            if path.stat().st_size > max_file_bytes:
                continue
            text = path.read_text(errors='replace')
        except OSError:
            continue
        rel = path.relative_to(root)
        rel_str = rel.as_posix()
        folder = rel.parent.as_posix() if rel.parent.as_posix() != '.' else '.'
        desc = _describe_file(rel, text)
        files[rel_str] = {'path': rel_str, 'folder': folder, 'description': desc, 'text': text}
        chunks[rel_str] = _split_chunks(rel_str, text)
        entry = folders.setdefault(folder, {'folder': folder, 'files': [], 'descriptions': []})
        entry['files'].append(rel_str)
        entry['descriptions'].append(desc)
    for folder, entry in folders.items():
        entry['summary'] = f"{folder} ({len(entry['files'])} files): " + '; '.join(entry['descriptions'][:5])
    return {'root': str(root), 'folders': folders, 'files': files, 'chunks': chunks}


def default_scorer(query, candidate_text):
    """Deterministic lexical P(yes) scorer in [0, 1] when no external decision model is supplied."""
    q_tokens = _tokens(query)
    if not q_tokens:
        return 0.0
    c_tokens = _tokens(candidate_text)
    overlap = len(q_tokens & c_tokens) / len(q_tokens)
    exact_file_bonus = 0.35 if any(tok in candidate_text.lower() for tok in q_tokens if '.' in tok or '_' in tok) else 0.0
    return min(1.0, round(overlap + exact_file_bonus, 4))


def grep_fallback(hierarchy, query, exclude_files=(), limit=3):
    """Recover files pruned by top-down folder selection that match query symbols or paths."""
    q_tokens = {t for t in _tokens(query) if len(t) >= 3}
    if not q_tokens:
        return []
    excluded = set(exclude_files or ())
    scored = []
    for rel_str, info in (hierarchy.get('files') or {}).items():
        if rel_str in excluded:
            continue
        haystack = (rel_str + '\n' + info.get('description', '') + '\n' + info.get('text', '')[:4000]).lower()
        hits = sum(1 for tok in q_tokens if tok in haystack)
        if hits > 0:
            scored.append((hits, rel_str))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [rel for _, rel in scored[:max(0, int(limit))]]


def navigate(repo_root, query, top_folders=2, top_files=5, top_chunks=5,
             fallback_limit=2, scorer=None, hierarchy=None):
    """Navigate `repo_root` top-down (folders -> files -> chunks) plus grep fallback.

    Returns `{'selected_folders', 'selected_files', 'recovered_files', 'selected_chunks', 'scores'}`.
    """
    scorer = scorer or default_scorer
    tree = hierarchy or build_hierarchy(repo_root)
    folder_scores = {
        folder: scorer(query, info['summary'])
        for folder, info in (tree.get('folders') or {}).items()
    }
    ranked_folders = [
        f for f, _ in sorted(folder_scores.items(), key=lambda kv: (-kv[1], kv[0]))[:max(1, int(top_folders))]
    ]
    candidate_files = [
        rel for f in ranked_folders for rel in (tree['folders'][f]['files'] if f in tree['folders'] else ())
    ]
    file_scores = {
        rel: scorer(query, tree['files'][rel]['description'] + '\n' + tree['files'][rel]['text'][:2000])
        for rel in candidate_files
    }
    selected_files = [
        rel for rel, _ in sorted(file_scores.items(), key=lambda kv: (-kv[1], kv[0]))[:max(1, int(top_files))]
    ]
    recovered = grep_fallback(tree, query, exclude_files=selected_files, limit=fallback_limit)
    final_files = list(dict.fromkeys(selected_files + recovered))

    chunk_candidates = []
    for rel in final_files:
        for ch in tree['chunks'].get(rel) or ():
            score = scorer(query, f"{ch['file']}:\n{ch['text']}")
            chunk_candidates.append((score, ch['file'], ch['start_line'], ch))
    chunk_candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    selected_chunks = [item[3] for item in chunk_candidates[:max(1, int(top_chunks))]]

    return {
        'selected_folders': ranked_folders,
        'selected_files': final_files,
        'recovered_files': recovered,
        'selected_chunks': selected_chunks,
        'scores': {'folders': folder_scores, 'files': file_scores},
    }


def compute_recall(selected_files, actual_changed_paths):
    """Recall of `actual_changed_paths` covered by `selected_files` (1.0 when actual is empty)."""
    actual = {str(p).strip() for p in (actual_changed_paths or ()) if str(p).strip()}
    if not actual:
        return 1.0
    selected = {str(p).strip() for p in (selected_files or ())}
    return round(len(actual & selected) / len(actual), 4)


def format_for_prompt(navigation):
    """Render selected files and chunks into a concise prompt block."""
    files = navigation.get('selected_files') or []
    if not files:
        return ''
    lines = ['## Repo context navigator (DecisionRAG)', 'Selected relevant files:']
    lines += [f'- `{path}`' for path in files]
    chunks = navigation.get('selected_chunks') or []
    if chunks:
        lines.append('\nRelevant code excerpts:')
        for ch in chunks[:3]:
            lines += [
                f"### `{ch['file']}` (lines {ch['start_line']}-{ch['end_line']})",
                '```',
                security.redact(ch['text']),
                '```',
            ]
    return '\n'.join(lines)
