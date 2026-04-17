export type AlignedWord = {
  text: string
  start: number
  end: number
}

export type AlignedLine = {
  text: string
  start: number
  end: number
  words: AlignedWord[]
}

export type AlignmentPayload = {
  schema_version: number
  lines: AlignedLine[]
  meta?: Record<string, unknown>
}
