// The preview renders agent-written markdown next to a write API, so every HTML
// string goes through DOMPurify (spec, Requirements). Never call `marked`
// without sanitising its output first.
import DOMPurify from 'dompurify'
import type { WindowLike } from 'dompurify'
import { marked } from 'marked'
import { extOf } from './fileIcons'

const purifiers = new WeakMap<WindowLike, ReturnType<typeof DOMPurify>>()

function purifierFor(win: WindowLike): ReturnType<typeof DOMPurify> {
  let purifier = purifiers.get(win)
  if (!purifier) {
    purifier = DOMPurify(win)
    purifiers.set(win, purifier)
  }
  return purifier
}

export function isMarkdownPath(path: string): boolean {
  return /^(md|markdown|mdc)$/.test(extOf(path))
}

export function renderMarkdown(text: string, win: WindowLike = window): string {
  const html = marked.parse(text, { async: false, gfm: true })
  return purifierFor(win).sanitize(html, { USE_PROFILES: { html: true } })
}
