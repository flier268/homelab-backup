export function useStableObjectKey(prefix: string): (value: object) => string {
  const keys = new WeakMap<object, string>()
  let sequence = 0

  return (value: object): string => {
    const existing = keys.get(value)
    if (existing) return existing
    sequence += 1
    const key = `${prefix}-${sequence}`
    keys.set(value, key)
    return key
  }
}
