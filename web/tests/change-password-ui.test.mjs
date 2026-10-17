/**
 * 改密码界面接线护栏（纯静态扫描，node --test 直接跑）：
 *
 *   1) 弹框组件存在，三个输入框全是 type=password，autocomplete 规范；
 *   2) api 层走 **POST body**（口令绝不进 URL / params / query）；
 *   3) App.vue 真挂上了这个弹框并有入口按钮；
 *   4) 🔴 口令不进 localStorage —— src 全扫，setItem 的键值里出现 password 就红。
 *
 * 第 (4) 条是这类界面最容易破的地方：顺手 `localStorage.setItem('pwd', ...)`
 * 「方便下次自动填」，等于把管理员口令长期留在浏览器里。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (rel) => readFileSync(join(SRC, rel), 'utf8')

const DIALOG = read('components/ChangePasswordDialog.vue')
const API = read('api/index.js')
const APP = read('App.vue')

/** 递归列出 src 下所有 .vue / .js */
function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) walk(p, out)
    else if (/\.(vue|js)$/.test(name)) out.push(p)
  }
  return out
}
const FILES = walk(SRC)

test('弹框：三个输入框全是 type=password，autocomplete 规范', () => {
  const inputs = [...DIALOG.matchAll(/<el-input\b[\s\S]*?\/>/g)].map((m) => m[0])
  assert.equal(inputs.length, 3, '旧 / 新 / 确认，三个字段')
  for (const tag of inputs) {
    assert.match(tag, /type="password"/, '口令框不许用明文类型')
  }
  // 旧密码是「当前」口令，两个新口令框都是 new-password（密码管理器据此不误填旧值）
  assert.match(DIALOG, /autocomplete="current-password"/)
  assert.equal([...DIALOG.matchAll(/autocomplete="new-password"/g)].length, 2)
})

test('弹框：遮罩误点不许当成确认（close-on-click-modal=false）', () => {
  assert.match(DIALOG, /:close-on-click-modal="false"/)
})

test('弹框：前端先校两次新密码一致 + 强度，后端仍是权威', () => {
  assert.match(DIALOG, /newPassword\.value !== confirmPassword\.value/)
  assert.match(DIALOG, /newPassword\.value\.length < MIN_LEN/)
  // 后端校验没被前端替代：请求失败走 catch，文案用后端给的
  assert.match(DIALOG, /catch \(e\) \{\s*\n\s*\/\/ 403 = 旧密码不对/)
})

test('弹框：成功 toast 用约定文案，关掉即清空三个 ref', () => {
  assert.match(DIALOG, /showToast\('密码已修改，其他设备需重新登录'\)/)
  const forget = DIALOG.match(/function forget\(\) \{[\s\S]*?\n\}/)[0]
  for (const field of ['oldPassword', 'newPassword', 'confirmPassword']) {
    assert.ok(forget.includes(`${field}.value = ''`), `${field} 没被清空`)
  }
})

test('api：口令走 POST body，绝不进 URL / params', () => {
  const fn = API.match(/changePassword:[\s\S]*?\.then\(\(r\) => r\.data\)/)[0]
  assert.match(fn, /http\s*\n?\s*\.post\('\/api\/v1\/auth\/change_password'/)
  assert.match(fn, /old_password: oldPassword/)
  assert.match(fn, /new_password: newPassword/)
  assert.match(fn, /confirm: true/)
  // 🔴 params 会把口令拼进 query string（进浏览器历史 / 访问日志 / Referer）
  assert.doesNotMatch(fn, /params/)
  assert.doesNotMatch(fn, /\?/)
})

test('api：拦截器把状态码挂到 Error 上（前端按码分流，不匹配中文文案）', () => {
  // 只钉机制（把 status 挂到 reject 出去的 Error 上），不钉局部变量名
  const interceptor = API.match(/http\.interceptors\.response\.use\([\s\S]*?\n\)/)[0]
  assert.match(interceptor, /\.status = status/)
  assert.match(interceptor, /Promise\.reject\(/)
})

test('App.vue：侧栏底部有入口并挂上了弹框', () => {
  assert.match(APP, /import ChangePasswordDialog from '\.\/components\/ChangePasswordDialog\.vue'/)
  assert.match(APP, /<ChangePasswordDialog v-model="pwdOpen" \/>/)
  assert.match(APP, /修改密码/)
  // 入口在 nav-foot（动作），不是 GROUPS 里的一页 —— 面包屑不该多出一项
  const foot = APP.indexOf('class="nav-foot"')
  assert.ok(foot > 0 && APP.indexOf('修改密码') > foot, '入口应在侧栏底部 nav-foot 内')
  const groups = APP.slice(APP.indexOf('const GROUPS'), APP.indexOf('const crumb'))
  assert.doesNotMatch(groups, /修改密码/, '改密码不是导航页，不许进 GROUPS')
})

test('🔴 口令零落盘：src 里没有把 password 写进 localStorage', () => {
  const bad = []
  for (const f of FILES) {
    const src = readFileSync(f, 'utf8')
    for (const [i, line] of src.split('\n').entries()) {
      if (!/localStorage|setItem/.test(line)) continue
      if (/password|pwd|old_password|new_password/i.test(line)) {
        bad.push(`${relative(SRC, f)}:${i + 1} ${line.trim()}`)
      }
    }
  }
  assert.deepEqual(bad, [], '口令不得进 localStorage')
})

test('🔴 口令零进 URL：改密相关的 URL 拼接里没有口令变量', () => {
  const bad = []
  for (const f of FILES) {
    const src = readFileSync(f, 'utf8')
    for (const [i, line] of src.split('\n').entries()) {
      // 只有拼接进 URL 路径/query 的才算漏（模板串里带 password 变量）
      if (!/`[^`]*\$\{[^}]*[Pp]assword/.test(line) && !/params:.*[Pp]assword/.test(line)) continue
      bad.push(`${relative(SRC, f)}:${i + 1} ${line.trim()}`)
    }
  }
  assert.deepEqual(bad, [], '口令不得进 URL')
})
