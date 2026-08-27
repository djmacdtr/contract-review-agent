import type { StampImage } from '../api/types'

export const STAMP_IMAGE_DISCLAIMER = '印章影像由文档智能解析识别，仅供业务人员查看，不作为印章真伪或法律效力判断依据。'

export function formatStampImageLocation(item: StampImage): string {
  return `《${item.file_name}》 · 第 ${item.page} 页`
}

export function isSafeStampImageSource(value: string): boolean {
  return /^data:image\/(?:png|jpeg);base64,[A-Za-z0-9+/]+={0,2}$/u.test(value)
}
