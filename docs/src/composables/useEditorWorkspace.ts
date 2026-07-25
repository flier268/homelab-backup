import { computed, ref } from 'vue'
import { createGlobalConfig, createServiceManifest } from '../domain/defaults'
import type { DocumentKind, EditorDocument } from '../domain/types'
import { validateDocument } from '../domain/validation'
import { parseEditorDocument, serializeEditorDocument } from '../domain/yaml'

export function useEditorWorkspace() {
  const document = ref<EditorDocument>({
    kind: 'service',
    value: createServiceManifest(),
  })
  const importError = ref('')
  const issues = computed(() => validateDocument(document.value))
  const yaml = computed(() => serializeEditorDocument(document.value))

  function create(kind: DocumentKind): void {
    importError.value = ''
    document.value = kind === 'global'
      ? { kind, value: createGlobalConfig() }
      : { kind, value: createServiceManifest() }
  }

  function importYaml(source: string): void {
    importError.value = ''
    try {
      document.value = parseEditorDocument(source)
    } catch (error) {
      importError.value = error instanceof Error ? error.message : String(error)
      throw error
    }
  }

  return { document, importError, issues, yaml, create, importYaml }
}
