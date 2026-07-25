// @vitest-environment happy-dom

import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { describe, expect, it } from 'vitest'
import ActionPhaseEditor from '../src/components/service/ActionPhaseEditor.vue'
import SourcesEditor from '../src/components/service/SourcesEditor.vue'
import { createServiceManifest } from '../src/domain/defaults'
import type { ActionDefinition } from '../src/domain/types'

const global = {
  plugins: [ElementPlus],
}

describe('editable collection identity', () => {
  it('does not remount a source card when its editable ID changes', async () => {
    const manifest = createServiceManifest()
    const wrapper = mount(SourcesEditor, {
      props: {
        modelValue: manifest,
        'onUpdate:modelValue': () => undefined,
      },
      global,
    })
    const originalCard = wrapper.find('.source-card').element

    await wrapper.find('.source-card input').setValue('renamed-source')

    expect(wrapper.find('.source-card').element).toBe(originalCard)
  })

  it('does not remount an action card when its editable name changes', async () => {
    const actions: ActionDefinition[] = [{
      name: 'notify',
      command: ['/usr/bin/true'],
      run_as: 'root',
    }]
    const wrapper = mount(ActionPhaseEditor, {
      props: {
        modelValue: actions,
        phase: 'before',
        title: '備份前',
        description: '測試',
        'onUpdate:modelValue': () => undefined,
      },
      global,
    })
    const originalCard = wrapper.find('.action-card').element

    await wrapper.find('.action-card input').setValue('renamed-action')

    expect(wrapper.find('.action-card').element).toBe(originalCard)
  })
})
