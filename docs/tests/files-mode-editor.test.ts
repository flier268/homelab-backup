// @vitest-environment happy-dom

import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { afterEach, describe, expect, it, vi } from 'vitest'
import ConsistencyActionsEditor from '../src/components/service/ConsistencyActionsEditor.vue'
import ServiceOverviewEditor from '../src/components/service/ServiceOverviewEditor.vue'
import SourcesEditor from '../src/components/service/SourcesEditor.vue'
import { createServiceManifest } from '../src/domain/defaults'

const global = {
  plugins: [ElementPlus],
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('files mode editor', () => {
  it('confirms before removing Compose and volume settings', async () => {
    const manifest = createServiceManifest()
    manifest.sources!.volumes.push({
      id: 'data', name: 'demo_data', required: true,
    })
    const confirm = vi.fn(() => false)
    Object.defineProperty(window, 'confirm', {
      configurable: true,
      value: confirm,
    })
    const wrapper = mount(ConsistencyActionsEditor, {
      props: {
        modelValue: manifest,
        'onUpdate:modelValue': () => undefined,
      },
      global,
    })
    const filesButton = wrapper.findAll('button').find(
      (button) => button.text().includes('純檔案'),
    )!

    await filesButton.trigger('click')
    expect(confirm).toHaveBeenCalledOnce()
    expect(manifest.consistency!.mode).toBe('stop')
    expect(manifest.compose).toBeDefined()
    expect(manifest.sources!.volumes).toHaveLength(1)

    confirm.mockReturnValue(true)
    await filesButton.trigger('click')
    expect(manifest.consistency!.mode).toBe('files')
    expect(manifest.compose).toBeUndefined()
    expect(manifest.sources!.volumes).toEqual([])

    const stopButton = wrapper.findAll('button').find(
      (button) => button.text().includes('Stop'),
    )!
    await stopButton.trigger('click')
    expect(manifest.consistency!.mode).toBe('stop')
    expect(manifest.compose).toEqual({ files: ['compose.yaml'] })
  })

  it('hides Docker-only editors in files mode', () => {
    const manifest = createServiceManifest()
    manifest.consistency!.mode = 'files'
    delete manifest.compose
    manifest.sources!.volumes = []

    const overview = mount(ServiceOverviewEditor, {
      props: {
        modelValue: manifest,
        'onUpdate:modelValue': () => undefined,
      },
      global,
    })
    const sources = mount(SourcesEditor, {
      props: {
        modelValue: manifest,
        'onUpdate:modelValue': () => undefined,
      },
      global,
    })

    expect(overview.text()).not.toContain('Compose files')
    expect(overview.text()).toContain('純檔案模式沒有 Docker Compose')
    expect(sources.text()).toContain('檔案與目錄')
    expect(sources.text()).not.toContain('Named volumes')
  })
})
