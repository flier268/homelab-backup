import { computed, ref } from 'vue'
import { createGlobalConfig, createServiceManifest } from '../domain/defaults'
import type { DocumentKind, EditorDocument } from '../domain/types'
import { validateDocument } from '../domain/validation'
import {
  hasLegacyActionSyntax,
  parseEditorDocument,
  serializeEditorDocument,
} from '../domain/yaml'

export function useEditorWorkspace() {
  const document = ref<EditorDocument>({
    kind: 'service',
    value: createServiceManifest(),
  })
  const importError = ref('')
  const importWarning = ref('')
  const issues = computed(() => validateDocument(document.value))
  const yaml = computed(() => serializeEditorDocument(document.value))

  function create(kind: DocumentKind): void {
    importError.value = ''
    importWarning.value = ''
    document.value = kind === 'global'
      ? { kind, value: createGlobalConfig() }
      : { kind, value: createServiceManifest() }
  }

  function importYaml(source: string): void {
    importError.value = ''
    importWarning.value = ''
    const legacyActions = hasLegacyActionSyntax(source)
    try {
      document.value = parseEditorDocument(source)
      if (legacyActions) {
        importWarning.value = (
          '已將舊版 actions／consistency hooks 轉成 actions.backup；'
          + '請檢查由 consistency.timeout 帶入的 action timeout。'
        )
      }
    } catch (error) {
      importWarning.value = ''
      importError.value = error instanceof Error ? error.message : String(error)
      throw error
    }
  }

  return {
    document, importError, importWarning, issues, yaml, create, importYaml,
  }
}
