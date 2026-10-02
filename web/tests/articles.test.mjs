import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../src/api.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } })
const { articleKey, sortedArticles, exportMarkdown } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)

test('distinct reports at one release page remain visible and exportable', () => {
  const common = { url: 'https://example.com/release', source: 'Google 技术报告', published_at: '2026-09-29' }
  const framework = { ...common, id: 'report-framework', title: 'Security framework', content_kind: '技术报告总结' }
  const roadmap = { ...common, id: 'report-roadmap', title: 'Control roadmap', content_kind: '技术报告总结' }
  const news = { ...common, id: 'news-new', title: 'Release news', content_kind: '摘要' }
  const items = sortedArticles([framework, roadmap, news,
    { ...framework, url: common.url + '#paper' },
    { ...news, id: 'news-old', url: common.url + '#announcement' },
    { ...roadmap, id: 'invalid', url: 'javascript:alert(1)' }])
  assert.equal(items.length, 3)
  assert.equal(new Set(items.map(articleKey)).size, 3)
  assert.deepEqual(new Set(items.map(item => item.id)), new Set(['report-framework', 'report-roadmap', 'news-new']))
  const markdown = exportMarkdown({ role: 'assistant', content: '检索结果', results: items })
  assert.match(markdown, /相关资讯 · 3 条/)
  assert.match(markdown, /Security framework/)
  assert.match(markdown, /Control roadmap/)
})
