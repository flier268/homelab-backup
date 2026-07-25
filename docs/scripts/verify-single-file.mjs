import { copyFile, readdir, readFile, rename } from 'node:fs/promises'
import { join } from 'node:path'

const outputDirectory = new URL('../dist/', import.meta.url)
const repositoryGuide = new URL('../../GUIDE.html', import.meta.url)
const files = await readdir(outputDirectory)

if (files.length !== 1 || files[0] !== 'index.html') {
  throw new Error(`expected exactly dist/index.html, found: ${files.join(', ')}`)
}

const source = join(outputDirectory.pathname, 'index.html')
const html = await readFile(source, 'utf8')

if (/<script[^>]+src=|<link[^>]+rel=["']stylesheet/i.test(html)) {
  throw new Error('single-file build still contains external script or stylesheet')
}
if (/(?:src|href)=["']https?:|url\(\s*["']?https?:/i.test(html)) {
  throw new Error('single-file build references an external HTTP(S) asset')
}

const builtGuide = join(outputDirectory.pathname, 'GUIDE.html')
await rename(source, builtGuide)
await copyFile(builtGuide, repositoryGuide)
