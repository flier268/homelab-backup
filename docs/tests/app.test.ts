// @vitest-environment happy-dom

import { afterEach, describe, expect, it } from 'vitest'
import { mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { nextTick } from 'vue'
import App from '../src/App.vue'
import { parseHash, routeHash } from '../src/composables/useHashNavigation'

let wrapper: VueWrapper | undefined

afterEach(() => {
  wrapper?.unmount()
  wrapper = undefined
})

describe('App', () => {
  it('mounts the service editor and switches to the global editor', async () => {
    window.location.hash = '#/editor/overview'
    wrapper = mount(App, {
      attachTo: document.body,
      global: {
        plugins: [ElementPlus],
      },
    })

    expect(wrapper.text()).toContain('基本資料與排程')
    expect(wrapper.text()).toContain('服務識別')

    const globalButton = wrapper.findAll('button').find(
      (button) => button.text().includes('全域設定'),
    )
    expect(globalButton).toBeDefined()
    await globalButton!.trigger('click')
    await nextTick()

    expect(wrapper.text()).toContain('主機與服務位置')
    expect(wrapper.text()).toContain('Trusted data roots')
  })

  it('switches between the integrated editor and guide', async () => {
    window.location.hash = '#/editor/overview'
    wrapper = mount(App, {
      attachTo: document.body,
      global: {
        plugins: [ElementPlus],
      },
    })

    const guideButton = wrapper.findAll('button').find(
      (button) => button.text().includes('完整指南'),
    )
    await guideButton!.trigger('click')
    window.dispatchEvent(new HashChangeEvent('hashchange'))
    await nextTick()

    expect(wrapper.text()).toContain('部署與維運指南')
    expect(wrapper.text()).toContain('從部署到災難復原')
    expect(wrapper.text()).toContain('一致性模式')

    const editorButton = wrapper.findAll('button').find(
      (button) => button.text().includes('設定編輯器'),
    )
    await editorButton!.trigger('click')
    window.dispatchEvent(new HashChangeEvent('hashchange'))
    await nextTick()
    expect(wrapper.text()).toContain('服務識別')
  })

  it('preserves unsaved editor state while viewing the guide', async () => {
    window.location.hash = '#/editor/overview'
    wrapper = mount(App, {
      attachTo: document.body,
      global: {
        plugins: [ElementPlus],
      },
    })

    const serviceInput = wrapper.find('input[placeholder="minecraft"]')
    await serviceInput.setValue('custom-service')

    const guideButton = wrapper.findAll('button').find(
      (button) => button.text().includes('完整指南'),
    )
    await guideButton!.trigger('click')
    window.dispatchEvent(new HashChangeEvent('hashchange'))
    await nextTick()

    const editorButton = wrapper.findAll('button').find(
      (button) => button.text().includes('設定編輯器'),
    )
    await editorButton!.trigger('click')
    window.dispatchEvent(new HashChangeEvent('hashchange'))
    await nextTick()

    expect(wrapper.find('input[placeholder="minecraft"]').element)
      .toHaveProperty('value', 'custom-service')
  })

  it('does not let the hidden editor rewrite guide navigation', async () => {
    window.location.hash = '#/editor/overview'
    wrapper = mount(App, {
      attachTo: document.body,
      global: {
        plugins: [ElementPlus],
      },
    })

    window.location.hash = '#/guide/goal'
    window.dispatchEvent(new HashChangeEvent('hashchange'))
    await nextTick()
    window.location.hash = '#/guide/sources'
    window.dispatchEvent(new HashChangeEvent('hashchange'))
    await nextTick()
    await nextTick()

    expect(window.location.hash).toBe('#/guide/sources')
  })

  it('opens the editor section associated with a guide chapter', async () => {
    window.location.hash = '#/guide/sources'
    wrapper = mount(App, {
      attachTo: document.body,
      global: {
        plugins: [ElementPlus],
      },
    })

    expect(wrapper.text()).toContain('Bind mount 與 named volume')
    const relatedEditorButton = wrapper.findAll('button').find(
      (button) => button.text().includes('前往對應編輯器'),
    )
    expect(relatedEditorButton).toBeDefined()
    await relatedEditorButton!.trigger('click')
    window.dispatchEvent(new HashChangeEvent('hashchange'))
    await nextTick()

    expect(window.location.hash).toBe('#/editor/sources')
    expect(wrapper.text()).toContain('從檔案清單建立來源群組')
  })

  it('defaults the unified page to the editor and creates stable hashes', () => {
    window.history.replaceState(null, '', '/GUIDE.html')
    expect(parseHash('')).toEqual({ view: 'editor', section: 'overview' })
    expect(routeHash('guide', 'global-config')).toBe('#/guide/global-config')
    expect(routeHash('editor', 'actions')).toBe('#/editor/actions')
  })
})
