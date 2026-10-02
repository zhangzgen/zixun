export type Article = {
  id: string
  title: string
  url: string
  source: string
  published_at: string
  summary?: string
  citation?: string
  content_kind?: string
}
export type Message = {
  role: 'user' | 'assistant'
  content: string
  sources?: Article[]
  results?: Article[]
  error?: string
}

export function sortedArticles(items: Article[]) {
  const unique = new Map<string, Article>()
  for (const item of items) {
    try {
      const url = new URL(item.url)
      if (!/^https?:$/.test(url.protocol)) continue
      url.hash = ''
      const key = `${url.href}|${item.content_kind === '技术报告总结' ? 'report' : 'news'}`
      if (!unique.has(key)) unique.set(key, item)
    } catch { /* Ignore an invalid archive URL rather than rendering a broken link. */ }
  }
  return [...unique.values()].sort((a, b) => {
    return b.published_at.localeCompare(a.published_at) || a.title.localeCompare(b.title, 'zh-CN')
  })
}

export async function getJSON<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { signal })
  if (!response.ok) throw new Error('暂时无法连接服务')
  return response.json()
}

export async function chatStream(body: object, signal: AbortSignal, onEvent: (event: string, data: Record<string, unknown>) => void) {
  const response = await fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal })
  if (!response.ok || !response.body) throw new Error(response.status === 503 ? '当前问答较多，请稍后重试' : '无法开始问答，请检查问题长度')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let terminal = false
  function parse(frame: string) {
    const event = frame.split('\n').find(line => line.startsWith('event:'))?.slice(6).trim()
    const data = frame.split('\n').filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n')
    if (event && data) {
      onEvent(event, JSON.parse(data))
      terminal ||= event === 'done' || event === 'error'
    }
  }
  try {
    while (true) {
      const { done, value } = await reader.read()
      buffer += decoder.decode(value, { stream: !done }).replace(/\r\n/g, '\n')
      let boundary: number
      while ((boundary = buffer.indexOf('\n\n')) >= 0) {
        parse(buffer.slice(0, boundary))
        buffer = buffer.slice(boundary + 2)
      }
      if (done) { if (buffer.trim()) parse(buffer); break }
    }
    if (!terminal) throw new Error('连接已中断，请重新提问')
  } finally { reader.releaseLock() }
}

export function exportMarkdown(message: Message) {
  const answer = message.content.replace(/\[([^\]]+)\]\(news:(N\d+)\)/g, (_match, label, citation) => {
    const article = message.sources?.find(item => item.citation === citation)
    return article ? `[${label}](${article.url})` : label
  })
  const articles = sortedArticles(message.results || [])
  if (!articles.length) return answer
  const links = articles.map(article => {
    const title = article.title.replace(/\\/g, '\\\\').replace(/\[/g, '\\[').replace(/\]/g, '\\]').replace(/\s*\n\s*/g, ' ')
    const url = article.url.replace(/ /g, '%20').replace(/</g, '%3C').replace(/>/g, '%3E')
    const metadata = [article.source, article.published_at?.slice(0, 10)].filter(Boolean).join(' · ')
    return `- [${title}](<${url}>)${metadata ? ' — ' + metadata : ''}`
  }).join('\n')
  return [answer, `## 相关资讯 · ${articles.length} 条\n\n${links}`].filter(Boolean).join('\n\n')
}
