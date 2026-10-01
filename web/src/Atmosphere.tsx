import { useEffect, useRef } from 'react'

export default function Atmosphere() {
  const canvas = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const element = canvas.current!
    const context = element.getContext('2d')
    if (!context) return
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)')
    let width = 0, height = 0, frame = 0, time = 0, lastFrame = 0
    const pointer = { x: 0.5, y: 0.5 }
    const drift = { x: 0.5, y: 0.5 }
    const resize = () => {
      const ratio = Math.min(window.devicePixelRatio || 1, 2)
      width = element.clientWidth; height = element.clientHeight
      element.width = width * ratio; element.height = height * ratio
      context.setTransform(ratio, 0, 0, ratio, 0, 0)
    }
    const move = (event: PointerEvent) => {
      if (reduced.matches || !width || !height) return
      const rect = element.getBoundingClientRect()
      pointer.x = Math.max(0, Math.min(1, (event.clientX - rect.left) / width))
      pointer.y = Math.max(0, Math.min(1, (event.clientY - rect.top) / height))
    }
    const draw = () => {
      if (!width || !height) return
      context.clearRect(0, 0, width, height)
      const offsetX = (drift.x - 0.5) * 30
      const offsetY = (drift.y - 0.5) * 22
      const spread = Math.max(width, height * 0.85)
      const glows = [
        { x: width * 0.16 + Math.sin(time * 0.45) * 28, y: height * 0.23, radius: spread * 0.46, color: '123, 193, 220', alpha: 0.28 },
        { x: width * 0.82, y: height * 0.29 + Math.cos(time * 0.4) * 26, radius: spread * 0.43, color: '154, 208, 197', alpha: 0.27 },
        { x: width * 0.65 + Math.cos(time * 0.35) * 24, y: height * 0.91, radius: spread * 0.39, color: '172, 169, 221', alpha: 0.19 },
      ]
      for (const glow of glows) {
        const x = glow.x + offsetX, y = glow.y + offsetY
        const gradient = context.createRadialGradient(x, y, 0, x, y, glow.radius)
        gradient.addColorStop(0, `rgba(${glow.color}, ${glow.alpha})`)
        gradient.addColorStop(0.45, `rgba(${glow.color}, ${glow.alpha * 0.48})`)
        gradient.addColorStop(1, `rgba(${glow.color}, 0)`)
        context.fillStyle = gradient
        context.fillRect(0, 0, width, height)
      }
      const cx = width * 0.72 + offsetX
      const cy = height * 0.32 + offsetY
      for (let band = 0; band < 5; band++) {
        context.beginPath()
        for (let step = 0; step <= 180; step++) {
          const angle = step / 180 * Math.PI * 2
          const r = Math.min(width * 0.43, 550) + band * 27 + Math.sin(angle * 3 + time) * 12
          const x = cx + Math.cos(angle) * r
          const y = cy + Math.sin(angle) * r * 0.48 + Math.cos(angle) * r * 0.19
          if (!step) context.moveTo(x, y); else context.lineTo(x, y)
        }
        context.closePath()
        context.strokeStyle = `rgba(81, 131, 148, ${0.065 - band * 0.009})`
        context.lineWidth = 1
        context.stroke()
      }
      for (let i = 0; i < 38; i++) {
        const x = ((i * 197.33 + 31) % 997) / 997 * width + Math.sin(time * 0.6 + i) * 6 + offsetX * 0.35
        const y = ((i * 137.19 + 19) % 701) / 701 * height + Math.cos(time * 0.5 + i) * 5 + offsetY * 0.35
        const alpha = 0.07 + (Math.sin(time + i) + 1) * 0.045
        context.fillStyle = `rgba(64, 125, 143, ${alpha})`
        context.beginPath(); context.arc(x, y, i % 7 === 0 ? 1.5 : 0.9, 0, Math.PI * 2); context.fill()
      }
    }
    const animate = (timestamp: number) => {
      if (reduced.matches || document.hidden) return
      if (!lastFrame || timestamp - lastFrame >= 32) {
        const elapsed = lastFrame ? Math.min(timestamp - lastFrame, 64) : 32
        time += elapsed * 0.00012
        drift.x += (pointer.x - drift.x) * 0.045
        drift.y += (pointer.y - drift.y) * 0.045
        lastFrame = timestamp
        draw()
      }
      frame = requestAnimationFrame(animate)
    }
    const restart = () => {
      cancelAnimationFrame(frame)
      lastFrame = 0
      if (document.hidden) return
      if (reduced.matches) {
        drift.x = 0.5; drift.y = 0.5
      }
      draw()
      if (!reduced.matches) frame = requestAnimationFrame(animate)
    }
    const observer = new ResizeObserver(() => { resize(); if (!document.hidden) draw() })
    observer.observe(element); resize(); restart()
    window.addEventListener('pointermove', move)
    document.addEventListener('visibilitychange', restart)
    reduced.addEventListener('change', restart)
    return () => { cancelAnimationFrame(frame); observer.disconnect(); window.removeEventListener('pointermove', move); document.removeEventListener('visibilitychange', restart); reduced.removeEventListener('change', restart) }
  }, [])
  return <canvas className="atmosphere" ref={canvas} aria-hidden="true" />
}
