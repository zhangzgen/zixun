import { useEffect, useState, type ReactNode } from 'react'
import { Globe2 } from 'lucide-react'

export function websiteDetails(href: string) {
  try {
    const url = new URL(href)
    if (!/^https?:$/.test(url.protocol)) return null
    return { domain: url.hostname.replace(/^www\./, ''), favicon: url.origin + '/favicon.ico' }
  } catch { return null }
}

export default function SiteLink({ href, children, title, className = '' }: {
  href: string
  children: ReactNode
  title?: string
  className?: string
}) {
  const site = websiteDetails(href)
  const [failed, setFailed] = useState(false)
  useEffect(() => setFailed(false), [href])
  if (!site) return <span>{children}</span>

  return <a className={'site-link ' + className} href={href} title={title || site.domain}
    target="_blank" rel="noopener noreferrer">
    <span className="site-icon" aria-hidden="true">
      {failed
        ? /^[a-z\d]/i.test(site.domain)
          ? <span className="site-icon-fallback">{site.domain.charAt(0).toUpperCase()}</span>
          : <Globe2 size={14} />
        : <img src={site.favicon} alt="" width={16} height={16} loading="lazy" decoding="async"
          referrerPolicy="no-referrer" onError={() => setFailed(true)} />}
    </span>
    <span className="site-link-label">{children}</span>
  </a>
}
