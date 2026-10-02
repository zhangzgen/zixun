import { useEffect, useId, useRef, useState, type FormEvent } from 'react'
import { ArrowUp, Check, ChevronDown, Copy, LoaderCircle, Orbit, Plus, Square } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import Atmosphere from './Atmosphere'
import SiteLink, { websiteDetails } from './SiteLink'
import { articleKey, chatStream, exportMarkdown, getJSON, sortedArticles, type Article, type Message } from './api'

function Markdown({ message }: { message: Message }) {
  return <ReactMarkdown
    remarkPlugins={[remarkGfm]}
    urlTransform={url => /^(https?:\/\/|news:N\d+$)/i.test(url) ? url : ''}
    components={{
      a: ({ href, children }) => {
        if (href?.startsWith('news:')) {
          const article = message.sources?.find(item => item.citation === href.slice(5))
          return article
            ? <SiteLink className="citation" href={article.url} title={article.title}>{children}</SiteLink>
            : <span>{children}</span>
        }
        return href ? <SiteLink href={href}>{children}</SiteLink> : <span>{children}</span>
      },
      table: ({ children }) => <div className="table-scroll"><table>{children}</table></div>,
      img: ({ alt }) => <span>{alt}</span>,
    }}
  >{message.content}</ReactMarkdown>
}

function NewsResults({ items }: { items?: Article[] }) {
  const [expanded, setExpanded] = useState(false)
  const listId = useId()
  const articles = sortedArticles(items || [])
  if (!articles.length) return null
  const visibleArticles = expanded ? articles : articles.slice(0, 3)
  return <section className="news-results" aria-label="相关资讯">
    <div className="results-heading">
      <h2>相关资讯 <span>· {articles.length} 条</span></h2>
      {articles.length > 3 && <button type="button" className="results-toggle"
        aria-label={expanded ? '收起资讯' : `展开其余 ${articles.length - 3} 条资讯`}
        title={expanded ? '收起资讯' : '展开全部资讯'}
        aria-expanded={expanded} aria-controls={listId} onClick={() => setExpanded(value => !value)}>
        <ChevronDown size={17} aria-hidden="true" />
      </button>}
    </div>
    <ul className="result-list" id={listId}>
      {visibleArticles.map(article => <li key={articleKey(article)}>
        <SiteLink href={article.url} title={article.title} className="result-link">{article.title}</SiteLink>
        <div className="result-meta">{[article.source, article.published_at?.slice(0, 10), websiteDetails(article.url)?.domain]
          .filter(Boolean).join(' · ')}</div>
      </li>)}
    </ul>
  </section>
}

export default function App() {
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState('')
  const [ready, setReady] = useState<boolean | null>(null)
  const [notice, setNotice] = useState('')
  const [copied, setCopied] = useState<number>()
  const controller = useRef<AbortController | null>(null)
  const scrollArea = useRef<HTMLDivElement>(null)
  const textarea = useRef<HTMLTextAreaElement>(null)
  const nearBottom = useRef(true)
  const inFlight = useRef(false)

  useEffect(() => {
    const abort = new AbortController()
    getJSON<{ model_ready: boolean }>('/api/health', abort.signal)
      .then(data => setReady(data.model_ready))
      .catch(() => { if (!abort.signal.aborted) setNotice('暂时无法连接服务，请稍后刷新。') })
    return () => { abort.abort(); controller.current?.abort() }
  }, [])

  useEffect(() => {
    if (nearBottom.current && scrollArea.current) scrollArea.current.scrollTop = scrollArea.current.scrollHeight
  }, [messages, status])

  useEffect(() => {
    if (textarea.current) {
      textarea.current.style.height = 'auto'
      textarea.current.style.height = Math.min(textarea.current.scrollHeight, 140) + 'px'
    }
  }, [input])

  function updateAnswer(updater: (message: Message) => Message) {
    setMessages(items => items.map((message, index) => index === items.length - 1 ? updater(message) : message))
  }

  async function send(event: FormEvent) {
    event.preventDefault()
    const text = input.trim()
    if (!text || inFlight.current) return
    if (!ready) { setNotice('问答服务尚未连接，请稍后再试。'); return }
    inFlight.current = true
    const history = messages.filter(item => item.content && !item.error).slice(-10)
      .map(item => ({ role: item.role, content: (item.role === 'assistant' ? exportMarkdown(item) : item.content).slice(0, 6000) }))
    setMessages(items => [...items, { role: 'user', content: text }, { role: 'assistant', content: '', sources: [], results: [] }])
    setBusy(true); setStatus('正在寻找线索'); setInput(''); setNotice('')
    nearBottom.current = true
    controller.current = new AbortController()
    try {
      await chatStream({ message: text, history }, controller.current.signal, (event, data) => {
        if (event === 'delta') updateAnswer(item => ({ ...item, content: item.content + String(data.text) }))
        if (event === 'reset_answer') updateAnswer(item => ({ ...item, content: '' }))
        if (event === 'status') setStatus(String(data.label))
        if (event === 'sources') updateAnswer(item => ({ ...item, sources: data.items as Article[] }))
        if (event === 'results' && Array.isArray(data.items)) updateAnswer(item => ({ ...item, results: data.items as Article[] }))
        if (event === 'done') updateAnswer(item => ({ ...item,
          sources: Array.isArray(data.sources) ? data.sources as Article[] : item.sources,
          results: Array.isArray(data.results) ? data.results as Article[] : item.results,
        }))
        if (event === 'error') updateAnswer(item => ({ ...item, error: String(data.message) }))
      })
    } catch (error) {
      updateAnswer(item => ({ ...item, error: (error as Error).name === 'AbortError' ? '已停止回答' : (error as Error).message }))
    } finally { inFlight.current = false; setBusy(false); setStatus('') }
  }

  function clear() {
    if (inFlight.current) return
    setMessages([]); setInput(''); setNotice(''); textarea.current?.focus()
  }

  async function copy(message: Message, index: number) {
    try {
      await navigator.clipboard.writeText(exportMarkdown(message))
      setCopied(index); setTimeout(() => setCopied(undefined), 1800)
    } catch { setNotice('浏览器暂不支持复制，请手动选择文字。') }
  }

  return <div className="app-shell">
    <Atmosphere />
    <header className="header">
      <a className="brand" href="/" onClick={event => { event.preventDefault(); clear() }}>
        <span className="brand-mark"><Orbit size={24} /></span>
        <div><strong>资讯之间</strong><span>在历史里，找到答案</span></div>
      </a>
      <button className="new-chat" disabled={busy || !messages.length} onClick={clear} aria-label="清空当前对话">
        <Plus size={16} /><span>新对话</span>
      </button>
    </header>

    <main className="chat-window">
      <div className="chat-scroll" ref={scrollArea} onScroll={() => {
        const element = scrollArea.current!
        nearBottom.current = element.scrollHeight - element.scrollTop - element.clientHeight < 100
      }}>
        {messages.length === 0 ? <div className="welcome">
          <div className="welcome-symbol"><Orbit size={38} strokeWidth={1} /><span /><span /></div>
          <div className="eyebrow">A QUESTION. A CONNECTION.</div>
          <h1>让线索，<span>成为洞见。</span></h1>
          <p>问一个问题，在历史资讯里找到有据可查的答案。</p>
        </div> : <div className="messages">
          {messages.map((message, index) => <section className={'message ' + message.role} key={index}>
            {message.role === 'user' ? <div className="user-bubble">{message.content}</div> : <>
              <div className="assistant-label"><Orbit size={17} /><span>资讯之间</span></div>
              <div className="markdown"><Markdown message={message} /></div>
              <NewsResults items={message.results} />
              {busy && index === messages.length - 1 && <div className="progress" role="status"><LoaderCircle size={13} /><span>{status}</span></div>}
              {message.error && <p className="error" role="status">{message.error}</p>}
              {(message.content || !!message.results?.length) && (!busy || index < messages.length - 1) && <button className="copy-answer" onClick={() => copy(message, index)} aria-label="复制回答和相关资讯链接">
                {copied === index ? <Check size={13} /> : <Copy size={13} />}<span>{copied === index ? '已复制' : '复制'}</span>
              </button>}
            </>}
          </section>)}
        </div>}
      </div>

      <div className="composer-wrap">
        {notice && <div className="notice" role="status">{notice}</div>}
        <form className="composer" onSubmit={send}>
          <textarea ref={textarea} rows={1} value={input} maxLength={3000}
            onChange={event => setInput(event.target.value)}
            placeholder="想查找什么资讯？" aria-label="向研究助手提问"
            onKeyDown={event => {
              if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault(); event.currentTarget.form?.requestSubmit()
              }
            }} />
          {busy ? <button className="send-button stop" type="button" onClick={() => controller.current?.abort()} aria-label="停止回答"><Square size={14} fill="currentColor" /></button>
            : <button className="send-button" type="submit" disabled={!input.trim()} aria-label="发送问题"><ArrowUp size={20} /></button>}
        </form>
        <div className="composer-note"><span>答案附原始来源 · 刷新即清空对话</span><span>Enter 发送 / Shift + Enter 换行</span></div>
      </div>
    </main>
  </div>
}
