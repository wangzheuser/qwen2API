/** 以无 Referer 方式打开或下载上游媒体，避免 CDN 防盗链拒绝业务域名。 */
export function openMediaLink(url: string, downloadName?: string) {
  const link = document.createElement("a")
  link.href = url
  if (downloadName) link.download = downloadName
  link.target = "_blank"
  link.rel = "noopener noreferrer"
  link.referrerPolicy = "no-referrer"
  link.click()
}
