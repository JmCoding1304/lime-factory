import { describe, expect, test } from 'bun:test'
import { JSDOM } from 'jsdom'
import type { WindowLike } from 'dompurify'
import { isMarkdownPath, renderMarkdown } from './markdown'

const win = new JSDOM('').window as unknown as WindowLike

describe('renderMarkdown', () => {
  test('renders headings and lists', () => {
    const html = renderMarkdown('# T\n\n- a', win)
    expect(html).toContain('<h1>T</h1>')
    expect(html).toContain('<li>a</li>')
  })

  test('script payload is inert', () => {
    const html = renderMarkdown(
      '<script>alert(1)</script>\n\n<div><script>x()</script></div>',
      win,
    )
    expect(html.toLowerCase()).not.toContain('<script')
  })

  test('onerror payload is inert', () => {
    const html = renderMarkdown(
      '<img src=x onerror="alert(1)">\n\ntext <img src=x onerror="alert(1)"> more',
      win,
    )
    expect(html.toLowerCase()).not.toContain('onerror')
  })

  test('javascript: links are inert', () => {
    const html = renderMarkdown(
      '[x](javascript:alert(1))\n\n<a href="JaVaScRiPt:alert(1)">y</a>\n\n<a href="  javascript:alert(1)">z</a>',
      win,
    )
    expect(html.toLowerCase()).not.toContain('javascript:')
  })

  test('output parses to an inert DOM', () => {
    const html = renderMarkdown(
      '# Title\n\n<script>alert(1)</script>\n\n<img src=x onerror="alert(1)">\n\n<a href="JaVaScRiPt:alert(1)">y</a>\n\n- item',
      win,
    )
    const doc = win.document as unknown as Document
    doc.body.innerHTML = html
    expect(doc.querySelectorAll('script').length).toBe(0)
    for (const el of Array.from(doc.querySelectorAll('*'))) {
      for (const attr of Array.from(el.attributes)) {
        expect(attr.name.startsWith('on')).toBe(false)
      }
    }
    for (const a of Array.from(doc.querySelectorAll('a[href]'))) {
      expect(/^\s*javascript:/i.test(a.getAttribute('href') ?? '')).toBe(false)
    }
  })
})

describe('isMarkdownPath', () => {
  test('recognises markdown extensions', () => {
    expect(isMarkdownPath('README.md')).toBe(true)
    expect(isMarkdownPath('a/b.MDC')).toBe(true)
    expect(isMarkdownPath('x.markdown')).toBe(true)
    expect(isMarkdownPath('x.ts')).toBe(false)
    expect(isMarkdownPath('md')).toBe(false)
  })
})
