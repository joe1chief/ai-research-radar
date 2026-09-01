import React, { useRef, useState, useEffect } from 'react'
import { type RadarEvent } from '../types'
import { scoreBand } from '../lib/radar'

interface ShareCardModalProps {
  event: RadarEvent
  onClose: () => void
}

export const ShareCardModal: React.FC<ShareCardModalProps> = ({ event, onClose }) => {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const [copied, setCopied] = useState(false)
  const [downloading, setDownloading] = useState(false)

  const band = scoreBand(event.score)

  const drawCard = () => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const width = 800
    const scale = 2
    canvas.width = width * scale
    ctx.scale(scale, scale)

    // Calculate dynamic height based on text
    const padding = 40
    let currentY = padding

    // Background
    ctx.fillStyle = '#0d1117'
    ctx.fillRect(0, 0, width, 1200)

    // Top subtle gradient bar
    const grad = ctx.createLinearGradient(0, 0, width, 0)
    grad.addColorStop(0, '#00d664')
    grad.addColorStop(0.5, '#00a562')
    grad.addColorStop(1, '#02894a')
    ctx.fillStyle = grad
    ctx.fillRect(0, 0, width, 6)

    // Header: Logo & Site Title
    currentY += 20
    ctx.fillStyle = '#00d664'
    ctx.font = 'bold 16px "Fira Code", monospace'
    ctx.fillText('⚡ AI RESEARCH RADAR', padding, currentY)

    // Score Badge
    const bandLabels: Record<string, string> = {
      alert: 'TOP BREAKTHROUGH',
      focus: 'HIGH IMPACT',
      standard: 'CORE SIGNAL',
      archive: 'TRACKED',
    }
    const scoreText = `${event.score} PTS · ${bandLabels[band] ?? 'SIGNAL'}`
    ctx.font = 'bold 14px "Fira Code", monospace'
    const scoreWidth = ctx.measureText(scoreText).width + 20
    ctx.fillStyle = '#161b22'
    ctx.strokeStyle = '#00d664'
    ctx.lineWidth = 1
    ctx.fillRect(width - padding - scoreWidth, currentY - 16, scoreWidth, 24)
    ctx.strokeRect(width - padding - scoreWidth, currentY - 16, scoreWidth, 24)
    ctx.fillStyle = '#00d664'
    ctx.fillText(scoreText, width - padding - scoreWidth + 10, currentY)

    // Divider
    currentY += 24
    ctx.strokeStyle = '#21262d'
    ctx.beginPath()
    ctx.moveTo(padding, currentY)
    ctx.lineTo(width - padding, currentY)
    ctx.stroke()

    // Topics & Meta
    currentY += 30
    ctx.fillStyle = '#8b949e'
    ctx.font = '13px "Fira Code", monospace'
    const topicText = event.topics.map(t => `#${t}`).join('  ')
    const dateText = event.published_at ? event.published_at.slice(0, 10) : ''
    ctx.fillText(`${topicText}  •  ${dateText}`, padding, currentY)

    // Title (wrapped)
    currentY += 36
    ctx.fillStyle = '#f0f6fc'
    ctx.font = 'bold 24px "Plus Jakarta Sans", "PingFang SC", sans-serif'
    currentY = wrapText(ctx, event.title_zh, padding, currentY, width - padding * 2, 34)

    // Summary Box
    currentY += 20
    ctx.fillStyle = '#161b22'
    ctx.strokeStyle = '#30363d'
    const summaryStartY = currentY
    ctx.font = '15px "PingFang SC", sans-serif'

    // We calculate summary height
    const dummyCanvas = document.createElement('canvas')
    const dummyCtx = dummyCanvas.getContext('2d')!
    dummyCtx.font = '15px "PingFang SC", sans-serif'
    const testY = wrapText(dummyCtx, event.summary_zh, padding + 20, summaryStartY + 30, width - padding * 2 - 40, 24)
    const boxHeight = testY - summaryStartY + 20

    ctx.fillRect(padding, summaryStartY, width - padding * 2, boxHeight)
    ctx.strokeRect(padding, summaryStartY, width - padding * 2, boxHeight)

    ctx.fillStyle = '#c9d1d9'
    wrapText(ctx, event.summary_zh, padding + 20, summaryStartY + 30, width - padding * 2 - 40, 24)

    currentY = summaryStartY + boxHeight + 25

    // Why it matters Box (accented)
    if (event.why_it_matters) {
      ctx.fillStyle = '#0a2316'
      ctx.strokeStyle = '#00d664'
      ctx.lineWidth = 1.5
      const whyStartY = currentY
      const testWhyY = wrapText(dummyCtx, `💡 战略影响: ${event.why_it_matters}`, padding + 20, whyStartY + 30, width - padding * 2 - 40, 24)
      const whyHeight = testWhyY - whyStartY + 20

      ctx.fillRect(padding, whyStartY, width - padding * 2, whyHeight)
      ctx.strokeRect(padding, whyStartY, width - padding * 2, whyHeight)

      ctx.fillStyle = '#7ee787'
      wrapText(ctx, `💡 战略影响: ${event.why_it_matters}`, padding + 20, whyStartY + 30, width - padding * 2 - 40, 24)

      currentY = whyStartY + whyHeight + 25
    }

    // Key quotes if available
    if (event.key_quotes && event.key_quotes.length > 0) {
      ctx.fillStyle = '#e3b341'
      ctx.font = 'bold 14px "PingFang SC", sans-serif'
      ctx.fillText('🎙️ 核心金句 / 机制推演:', padding, currentY)
      currentY += 24
      ctx.font = 'italic 14px "PingFang SC", sans-serif'
      ctx.fillStyle = '#d29922'
      for (const quote of event.key_quotes.slice(0, 2)) {
        currentY = wrapText(ctx, `“ ${quote} ”`, padding + 10, currentY, width - padding * 2 - 20, 22)
        currentY += 8
      }
      currentY += 15
    }

    // Footer Watermark
    currentY += 20
    ctx.strokeStyle = '#21262d'
    ctx.beginPath()
    ctx.moveTo(padding, currentY)
    ctx.lineTo(width - padding, currentY)
    ctx.stroke()

    currentY += 30
    ctx.fillStyle = '#58a6ff'
    ctx.font = '13px "Fira Code", monospace'
    ctx.fillText('📡 https://joe1chief.github.io/ai-research-radar/', padding, currentY)

    ctx.fillStyle = '#8b949e'
    ctx.font = '12px "PingFang SC", sans-serif'
    const rightNote = '全天候硅谷 AI 独角兽与深度研判'
    const rightWidth = ctx.measureText(rightNote).width
    ctx.fillText(rightNote, width - padding - rightWidth, currentY)

    currentY += 40

    // Resize canvas to exact fit
    const finalHeight = currentY
    const finalData = ctx.getImageData(0, 0, width * scale, finalHeight * scale)
    canvas.height = finalHeight * scale
    ctx.putImageData(finalData, 0, 0)
  }

  function wrapText(
    ctx: CanvasRenderingContext2D,
    text: string,
    x: number,
    y: number,
    maxWidth: number,
    lineHeight: number
  ): number {
    const chars = Array.from(text)
    let line = ''
    let curY = y

    for (let n = 0; n < chars.length; n++) {
      const testLine = line + chars[n]
      const metrics = ctx.measureText(testLine)
      const testWidth = metrics.width
      if (testWidth > maxWidth && n > 0) {
        ctx.fillText(line, x, curY)
        line = chars[n]
        curY += lineHeight
      } else {
        line = testLine
      }
    }
    ctx.fillText(line, x, curY)
    return curY + lineHeight
  }

  useEffect(() => {
    drawCard()
  }, [event])

  const downloadPNG = () => {
    const canvas = canvasRef.current
    if (!canvas) return
    setDownloading(true)
    const link = document.createElement('a')
    link.download = `ai-radar-${event.event_id}.png`
    link.href = canvas.toDataURL('image/png')
    link.click()
    setDownloading(false)
  }

  const copyText = async () => {
    const text = `🚨 [AI Radar · ${event.score}分] ${event.title_zh}\n\n📝 研判摘要：\n${event.summary_zh}\n\n💡 为什么重要：\n${event.why_it_matters}\n\n🔗 详情：https://joe1chief.github.io/ai-research-radar/`
    await navigator.clipboard.writeText(text)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm p-4 overflow-y-auto">
      <div className="bg-[#0d1117] border border-[#30363d] rounded-xl max-w-2xl w-full p-6 shadow-2xl space-y-4 my-8">
        <div className="flex items-center justify-between border-b border-[#21262d] pb-3">
          <div className="flex items-center space-x-2">
            <span className="inline-block w-2.5 h-2.5 rounded-full bg-[#00d664] animate-pulse"></span>
            <h3 className="text-white font-bold text-lg font-mono">生成分享海报卡片</h3>
          </div>
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-white text-xl font-bold p-1 rounded hover:bg-gray-800 transition"
          >
            ✕
          </button>
        </div>

        {/* Canvas Preview Container */}
        <div className="rounded-lg overflow-hidden border border-[#21262d] flex justify-center bg-black/40 p-2 max-h-[60vh] overflow-y-auto">
          <canvas
            ref={canvasRef}
            className="w-full max-w-[500px] h-auto rounded shadow-lg"
          />
        </div>

        {/* Action Buttons */}
        <div className="flex flex-wrap items-center justify-between gap-3 pt-2">
          <button
            onClick={copyText}
            className="px-4 py-2 bg-[#161b22] hover:bg-[#21262d] text-gray-200 border border-[#30363d] rounded-lg text-sm font-medium transition flex items-center space-x-2"
          >
            <span>{copied ? '✅ 已复制文案' : '📋 复制快讯文案'}</span>
          </button>
          <div className="flex items-center space-x-3">
            <button
              onClick={onClose}
              className="px-4 py-2 bg-transparent hover:bg-gray-800 text-gray-400 hover:text-white rounded-lg text-sm transition"
            >
              取消
            </button>
            <button
              onClick={downloadPNG}
              disabled={downloading}
              className="px-5 py-2 bg-[#00d664] hover:bg-[#00bf59] text-black font-bold rounded-lg text-sm shadow-md hover:shadow-emerald-500/20 transition flex items-center space-x-2"
            >
              <span>💾 下载高清海报 PNG</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
