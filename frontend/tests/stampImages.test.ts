import type { StampImage } from '../src/api/types.ts'
import { formatStampImageLocation, isSafeStampImageSource, STAMP_IMAGE_DISCLAIMER } from '../src/utils/stampImages.ts'

function assertEqual(actual: unknown, expected: unknown) {
  if (actual !== expected) throw new Error(`expected ${String(expected)}, got ${String(actual)}`)
}
function assert(condition: unknown) {
  if (!condition) throw new Error('assertion failed')
}

const item: StampImage = { file_name: '盖章合同.pdf', page: 3, data_uri: 'data:image/png;base64,iVBORw0KGgo=' }
assertEqual(formatStampImageLocation(item), '《盖章合同.pdf》 · 第 3 页')
assert(isSafeStampImageSource(item.data_uri))
assert(!isSafeStampImageSource('https://ocr.example/stamp.png'))
assert(STAMP_IMAGE_DISCLAIMER.includes('不作为印章真伪或法律效力判断依据'))
