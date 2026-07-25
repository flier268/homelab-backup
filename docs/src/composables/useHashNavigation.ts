import { computed, onBeforeUnmount, ref } from 'vue'

export type AppView = 'editor' | 'guide'
export type EditorSection = 'overview' | 'sources' | 'actions' | 'global'

export interface AppRoute {
  view: AppView
  section: string
}

export function parseHash(hash: string): AppRoute {
  const match = /^#\/(editor|guide)(?:\/([^/?#]+))?/.exec(hash)
  if (!match) return { view: 'editor', section: 'overview' }
  return {
    view: match[1] as AppView,
    section: decodeURIComponent(match[2] ?? ''),
  }
}

export function routeHash(view: AppView, section = ''): string {
  return `#/${view}${section ? `/${encodeURIComponent(section)}` : ''}`
}

export function useHashNavigation() {
  const currentHash = ref(window.location.hash)
  const route = computed(() => parseHash(currentHash.value))

  function sync(): void {
    currentHash.value = window.location.hash
  }

  function navigate(view: AppView, section = ''): void {
    const target = routeHash(view, section)
    if (window.location.hash === target) {
      currentHash.value = target
      return
    }
    window.location.hash = target
  }

  window.addEventListener('hashchange', sync)
  onBeforeUnmount(() => window.removeEventListener('hashchange', sync))

  return { route, navigate }
}
