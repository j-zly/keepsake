<!--
  知识库：默认视图是**文档列表**（不输词就有数据，P9 起）；P10 起检索是**两路并行** ——
  同一个词同时打 /kb/documents（按标题过滤下面的文档列表）和 /kb/search（切片正文命中）。
  两路各带各的分页与计数，结果行合成「文档 N 条 · 切片 M 条」；**任一有结果就算找得到**。
  清空 ✕（或删光后回车）两路都清、无条件回全量文档列表（不是空态）；点文档 → 抽屉拉它的切片。

  口径全部来自接口，页面不另算：count / page / page_size 都是后端给的。
  防串用请求序号（utils/kb.js 的 isStale）：快速改词时旧响应只丢弃、不覆盖新结果。

  批2 加了写操作：新增 / 编辑 / 真删 / 导入 / 导出，全部只对**手动域**文档开放
  （skill 域归夜间灌入器，前端不渲染按钮，后端 _manual_only 409 兜底）。
  手动域判定读列表接口真投影出来的 source_type，**严格判据**：字段缺席/未知一律
  按只读渲染（缺省放行会让 skill 文档看着也能改）。筛选区加了来源下拉，值原样
  透传后端 `source_type`（精确匹配，与导出端点同一套口径与校验）。

  内容类型（content_type）：**分内容类型**的 Tab（全部/技能/手记/文章/链接），
  选中即 GET /kb/documents?content_type=；每个 Tab 的计数来自 /kb/type-counts
  （后端给的真实数字，**绝不写死** —— 写死就等于库一变数字就错）。
  新增弹框可选类型：手记/文章（正文逐字切块）/链接（URL + 摘要，**单块不切**）。
  🔴 **编辑弹框不带类型**：类型创建后不可变（后端带异值 400），重切按文档自身
  类型走；前端这一层是「不给入口」，后端那一层是「给了也拒」，双挡。

  自由标签（tags）是**第三个维度**（content_type 是封闭枚举，标签是开放维度）：
  类型 Tab 下面一整行 chip 来自 GET /kb/tags（计数后端给，**前端零写死数字**），
  点选即按该标筛、再点取消；库里一个标签都没有时**整行隐藏**（不编 chip）。
  新增/编辑弹框里逗号或回车成一个 chip，超 12 个 / 单项超 32 字**即时提示**。
  标签与内容类型、来源三者取 AND；后端 update 是整体替换（不给 tags 就不动）。

  审核队列（批2）：**子代理**（非主代理）写进来的知识文档先进待审队列 —— 顶上一颗
  「审核」chip（计数来自 GET /kb/pending 的 pending_count / count，**前端零写死数字**，
  接口可达即显示：pending_count>0 红字「待审 {N}」、count>0 灰字「审核 · {count} 条历史」、
  都 0 灰字「审核 · 空」），点开是待审列表面板（标题/类型/来源代理/时间/正文预览/同名重复
  警示）。批准 = 此刻才切块进检索；驳回 = 只标状态不删正文（必填理由），两者都走
  ConfirmDialog 二次确认（与「记忆维护 › 常驻候选」审核同款，不造第三种交互）。
  🔴 站内手建与主代理写入**不经过这一关** —— 后端按「谁在写」判，前端不设第二道闸。
-->
<template>
  <div>
    <div class="page-head">
      <h1 class="page-title">知识库</h1>
      <p class="page-sub">PG 里的文档与切片；默认按文档浏览，输了词则文档标题与切片正文一起查。</p>
    </div>

    <div v-if="error" class="banner banner--danger">{{ error }}</div>

    <div class="stat-grid">
      <StatCard label="skill_docs" :value="summary?.skill_docs ?? '-'" unit="条" />
      <StatCard label="documents" :value="summary?.documents ?? '-'" unit="条" />
      <StatCard label="chunks" :value="summary?.chunks ?? '-'" unit="条" />
    </div>

    <div class="filter-panel">
      <!-- 审核队列 chip（批2）：始终渲染，**接口可达即显示**（不编假数字，也不藏入口）。
           计数全部来自 GET /kb/pending 的 pending_count / count（前端零写死）：
             · pending_count>0   → 红字 chip「待审 {N}」（强调真有未处理项）
             · pending_count==0 但 count>0 → 灰字「审核 · {count} 条历史」（只看驳回历史）
             · 都 0               → 灰字「审核 · 空」（点开仍是面板，看空态文案）
           点开 = 待审列表面板，批准/驳回都在那儿，与「记忆维护 › 常驻候选」审核区同款。
           🔴 侧栏徽标（App.vue /review/counts）仍维持「0 即隐藏」的语义，这里改了不影响。 -->
      <div class="filter-title">审核</div>
      <div class="tabs">
        <button
          class="tab"
          :class="{ 'tab--on': pendingOpen && pendCount > 0, 'chip--danger': pendCount > 0 }"
          @click="togglePending"
        >
          {{ chipText }}
        </button>
      </div>
      <!-- 内容类型 Tab：计数来自 /kb/type-counts（后端给的真实数字，不写死）；
           选中即 GET /kb/documents?content_type=，与 q / source_type 三者取 AND -->
      <div class="filter-title">内容类型</div>
      <div class="tabs">
        <button
          v-for="t in TYPE_TABS"
          :key="t.value"
          class="tab"
          :class="{ 'tab--on': contentType === t.value }"
          :title="t.hint"
          @click="onType(t.value)"
        >
          {{ t.label }}
          <span v-if="typeCounts[t.value] !== undefined" class="tab-n">{{ typeCounts[t.value] }}</span>
        </button>
      </div>
      <!-- 标签 chip 行：数据来自 /kb/tags（后端给的真实计数，前端零写死）。
           点选 = 按该标筛，再点取消；库里没标签时整行隐藏（v-if，不编 chip）。
           选中态复用 .tab--on 这套（零新色），与上方内容类型 Tab 同一份配方。 -->
      <template v-if="tagRows.length">
        <div class="filter-title">标签</div>
        <div class="tabs">
          <button
            v-for="row in tagRows"
            :key="row.tag"
            class="tab"
            :class="{ 'tab--on': activeTag === row.tag }"
            :title="`只列带「${row.tag}」标签的文档`"
            @click="onTag(row.tag)"
          >
            {{ row.tag }}
            <span class="tab-n">{{ row.count }}</span>
          </button>
        </div>
      </template>
      <div class="filter-title">检索</div>
      <p class="filter-desc">
        一个词两路同时查：下面的文档列表按标题过滤，切片区按正文命中；任一有结果就算找得到。清空即回到全量文档列表。类型 Tab 与标签 chip 按内容类型 / 标签筛（与下面的来源下拉、检索词四者取 AND）。
      </p>
      <div class="filter-ctl">
        <el-input
          v-model="q"
          class="grow"
          placeholder="输入检索词：文档标题与切片正文同时查"
          clearable
          @keyup.enter="search"
          @clear="onClear"
        >
          <template #prefix>
            <span class="search-ico">
              <svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor"
                stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="7" cy="7" r="4.3" />
                <path d="m10.3 10.3 3.4 3.4" />
              </svg>
            </span>
          </template>
        </el-input>
        <el-select v-model="sourceType" class="sel" title="来源" @change="onSource">
          <el-option label="全部来源" value="" />
          <el-option label="手动（可改删）" value="manual" />
          <el-option label="技能 skill（灌入器）" value="skill" />
          <el-option label="技能手册 skill_hk（灌入器）" value="skill_hk" />
        </el-select>
        <el-button type="primary" :loading="loading" @click="search">搜索</el-button>
      </div>
      <!-- 操作区：新增走弹框（只进手动域）；导入先预演再手输条数；导出走后端流式端点（带 Authorization 头） -->
      <div class="acts">
        <el-button :loading="busyExport" @click="doExport">导出</el-button>
        <el-button @click="openImport">导入</el-button>
        <el-button type="primary" @click="openCreate">新增文档</el-button>
      </div>
      <!-- 两路计数合并：N/M 来自各自接口的 count，不写死 -->
      <div v-if="searching && !loading" class="filter-result">
        <span>{{ mergeCounts(docCount, hitCount) }}</span>
      </div>
    </div>

    <!-- 待审面板（批2）：子代理写进来的知识文档先审后用。样式与「记忆维护 › 常驻候选」
         审核区同款（同一份 .panel/.card-grid/PagerBar 配方，零新色）。
         🔴 pending 文档此刻**一块切片都没有**，正文只能看 review_meta 的预览 ——
         这也是「未审正文不进检索索引」的另一面：它连自己都搜不到。 -->
    <section v-if="pendingOpen" class="panel pad">
      <div class="filter-title">待审队列（共 {{ pendTotal }} 条，其中待审 {{ pendCount }}）</div>
      <p class="filter-desc">
        子代理（非主代理）提交的知识文档一律先进这里，批准之后才切块进检索；驳回只标状态、不删正文，随时可以再审。站内手建与主代理写入不经过这一关。
      </p>
      <div v-if="pendError" class="banner banner--danger">{{ pendError }}</div>
      <div v-if="pendLoading && !pendRows.length" class="pad">
        <el-skeleton :rows="2" animated />
      </div>
      <div v-else-if="!pendError && !pendRows.length" class="empty">
        <p><b>没有待审的文档。</b>子代理提交的知识文档会出现在这里；<br />子代理经 MCP 提交的知识文档会进入这里。</p>
      </div>
      <div v-else class="card-grid">
        <ItemCard v-for="row in pendRows" :key="row.doc_id" chip-tone="accent">
          <template #tag>{{ row.status === 'rejected' ? '已驳回' : '待审' }}</template>
          <template v-if="row.submitted_at" #time>{{ toLocal(row.submitted_at) }}</template>

          <p class="card-title" :title="row.title">{{ row.title || '（无标题）' }}</p>
          <p class="card-body" :title="row.preview">{{ row.preview || '（没有提交正文）' }}</p>

          <template #tags>
            <span class="tag mono">{{ row.doc_id }}</span>
            <span class="tag" :class="`tag--${typeOf(row)}`">
              {{ typeLabel(row) }}<template v-if="typeOf(row) === 'link'"> ↗</template>
            </span>
            <span v-for="t in tagsOf(row)" :key="t" class="tag" :title="`标签：${t}`">{{ t }}</span>
            <span class="tag">来源 {{ row.submitted_by || '未知' }}</span>
            <!-- 查重提示：同名/近重复不拒收，但这篇得让人看见 -->
            <span v-if="row.dup_of" class="tag tag--link" :title="`与《${row.dup_of_title}》重复`">
              {{ row.dup_exact ? '同名重复' : '近重复' }} · {{ row.dup_of_title }}
            </span>
            <span v-if="row.reject_reason" class="tag">驳回理由：{{ row.reject_reason }}</span>
          </template>

          <template #actions>
            <button class="act-btn" :disabled="busyReview" @click="askApprove(row)">
              {{ row.status === 'rejected' ? '重新批准' : '批准入库' }}
            </button>
            <button
              v-if="row.status !== 'rejected'"
              class="act-btn act-btn--danger"
              :disabled="busyReview"
              @click="openReject(row)"
            >
              驳回
            </button>
          </template>
        </ItemCard>
      </div>
      <PagerBar
        v-if="pendRows.length"
        :total="pendTotal"
        :page="pendPage"
        :page-size="pendSize"
        @update:page="onPendPage"
        @update:page-size="onPendSize"
      />
    </section>

    <div v-if="loading" class="panel pad">
      <el-skeleton :rows="4" animated />
    </div>

    <!-- 两路都为 0 才算没找到（anyHit）；任一有结果往下走；出错时只留上方横幅，不冒充「没找到」 -->
    <div v-else-if="searching && !anyHit && !error" class="panel">
      <div class="empty">
        <svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" stroke-width="1.2">
          <path d="M4.5 4h4.6a2 2 0 0 1 2 2v13a1.8 1.8 0 0 0-1.8-1.8H4.5Z" stroke-linejoin="round" />
          <path d="M19.5 4h-4.6a2 2 0 0 0-2 2v13a1.8 1.8 0 0 0 1.8 1.8h4.8Z" stroke-linejoin="round" />
        </svg>
        <p><b>没找到「{{ q.trim() }}」</b>文档标题和切片正文都没命中 —— 换个更短的核心词试试。</p>
      </div>
    </div>

    <template v-else>
      <!-- 第一路：文档列表（检索态下按标题过滤，原地更新 doc-grid） -->
      <div v-if="searching && docs.length" class="section-title">文档</div>
      <div v-if="docs.length" class="card-grid">
        <ItemCard v-for="doc in docs" :key="doc.doc_id" @lead="openDoc(doc)">
          <template #tag>切片 {{ doc.chunk_count }}</template>
          <template v-if="doc.updated_at" #time>{{ toLocal(doc.updated_at) }}</template>

          <p class="card-title" :title="doc.title">{{ doc.title || '（无标题）' }}</p>
          <p class="card-body mono">{{ doc.doc_id }}</p>

          <template #tags>
            <span class="tag mono">{{ doc.doc_id }}</span>
            <!-- 类型徽标：技能=灰描边、链接=描边带 ↗（点得进去的意思），其余同现有 tag -->
            <span class="tag" :class="`tag--${typeOf(doc)}`">
              {{ typeLabel(doc) }}<template v-if="typeOf(doc) === 'link'"> ↗</template>
            </span>
            <!-- 自由标签：复用现有 .tag 配方（零新色）。字段缺席（老库未迁移）= 没打标 -->
            <span v-for="t in tagsOf(doc)" :key="t" class="tag" :title="`标签：${t}`">{{ t }}</span>
            <span v-if="!isManual(doc)" class="tag">只读 · 灌入器维护</span>
          </template>

          <template #lead>查看切片 →</template>
          <!-- 只有手动域能改/删；skill 域不渲染按钮（后端 409 兜底，这里先挡一道） -->
          <template v-if="isManual(doc)" #actions>
            <button class="act-btn" @click="openEdit(doc)">编辑</button>
            <button class="act-btn act-btn--danger" @click="askDelete(doc)">删除</button>
          </template>
        </ItemCard>
      </div>

      <!-- 「知识库里还没有文档」只在**请求成功且返回 0 条**时出现：加载中有骨架屏、出错有上方横幅 -->
      <div v-if="!docs.length && !searching && docLoaded && !error" class="panel">
        <div class="empty">
          <svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" stroke-width="1.2">
            <path d="M4.5 4h4.6a2 2 0 0 1 2 2v13a1.8 1.8 0 0 0-1.8-1.8H4.5Z" stroke-linejoin="round" />
            <path d="M19.5 4h-4.6a2 2 0 0 0-2 2v13a1.8 1.8 0 0 0 1.8 1.8h4.8Z" stroke-linejoin="round" />
          </svg>
          <p v-if="!sourceType && !contentType && !activeTag"><b>知识库里还没有文档</b>往 documents 表灌入文档后，这里会自动列出标题、更新时间和切片数。</p>
          <p v-else><b>该筛选下还没有文档</b>当前只列 {{ filterLabel }} 的文档，换个筛选或选「全部」再看看。</p>
        </div>
      </div>

      <PagerBar
        v-if="docCount"
        :total="docCount"
        :page="page"
        :page-size="pageSize"
        @update:page="onPage"
        @update:page-size="onSize"
      />

      <!-- 第二路：切片命中区（分页独立，走 /kb/search 的 page） -->
      <template v-if="searching">
        <div v-if="hits.length" class="section-title">切片</div>
        <div v-if="hits.length" class="card-grid">
          <ItemCard v-for="row in hits" :key="row.chunk_id" chip-tone="accent" @lead="doCopy(row.chunk_id)">
            <template #tag>rank {{ row.rank }}</template>

            <p class="card-title" :title="row.title">{{ row.title || '（无标题）' }}</p>
            <p class="card-body" :title="row.snippet">{{ row.snippet }}</p>

            <template #tags>
              <span class="tag mono">{{ row.chunk_id }}</span>
              <span class="tag" :class="`tag--${typeOf(row)}`">
                {{ typeLabel(row) }}<template v-if="typeOf(row) === 'link'"> ↗</template>
              </span>
              <span v-for="t in tagsOf(row)" :key="t" class="tag" :title="`标签：${t}`">{{ t }}</span>
            </template>

            <template #lead>复制 chunk_id ↗</template>
          </ItemCard>
        </div>

        <PagerBar
          v-if="hitCount"
          :total="hitCount"
          :page="hitPage"
          :page-size="hitSize"
          @update:page="onHitPage"
          @update:page-size="onHitSize"
        />
      </template>
    </template>

    <!-- 某个文档的切片 -->
    <el-drawer
      v-model="drawer"
      :title="`切片 · ${curDoc?.title || curDoc?.doc_id || ''}`"
      size="min(620px, 92vw)"
    >
      <div class="chunk-box">
        <el-skeleton v-if="chunkLoading" :rows="3" animated />
        <template v-else>
        <p class="chunk-count">
          共 {{ chunkCount }} 条切片 · 第 {{ chunkPage }} 页
        </p>
        <ol class="chunk-list">
          <li v-for="c in chunks" :key="c.chunk_id">
            <div class="chunk-top">
              <span class="tag mono">{{ c.chunk_id }}</span>
              <button class="card-link" @click="doCopy(c.chunk_id)">复制 ↗</button>
            </div>
            <p class="chunk-text">{{ c.snippet || '（空切片）' }}</p>
          </li>
        </ol>
        <p v-if="!chunks.length" class="chunk-count">这个文档下还没有切片。</p>
        <PagerBar
          v-if="chunkCount"
          :total="chunkCount"
          :page="chunkPage"
          :page-size="chunkSize"
          @update:page="onChunkPage"
          @update:page-size="onChunkSize"
        />
        </template>
      </div>
    </el-drawer>

    <!-- 新增：标题与正文都必填（后端任一为空直 400）；只进手动域，错误文案原样透出 -->
    <el-dialog
      v-model="creating"
      title="新增知识库文档"
      width="560px"
      align-center
      :close-on-click-modal="false"
      :close-on-press-escape="!busyCreate"
      :show-close="!busyCreate"
      append-to-body
    >
      <el-form label-position="top">
        <el-form-item label="内容类型">
          <el-select v-model="draftType" class="sel-full" title="内容类型（创建后不可改）">
            <el-option
              v-for="t in CREATE_TYPES"
              :key="t.value"
              :label="t.label"
              :value="t.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="标题（必填）">
          <el-input v-model="draftTitle" placeholder="一句话说清这篇文档是什么" />
        </el-form-item>
        <!-- 链接卡片：URL 必填 + 摘要（可空）。正文**不切**，整条存成一块 -->
        <el-form-item v-if="draftType === 'link'" label="链接地址（必填）">
          <el-input v-model="draftUrl" placeholder="https://example.com/page" />
        </el-form-item>
        <el-form-item :label="draftType === 'link' ? '摘要（可选）' : '正文（必填）'">
          <el-input
            v-model="draftContent"
            type="textarea"
            :rows="9"
            :placeholder="
              draftType === 'link'
                ? '两三句话说清这篇链接讲什么；留空也行'
                : '文档全文；保存时后端会切块并同事务落库'
            "
          />
        </el-form-item>
        <!-- 自由标签（可选）：逗号或回车各成一个 chip。超限即时提示，别等后端 400 -->
        <el-form-item label="标签（可选）">
          <el-select
            v-model="draftTags"
            class="sel-full"
            multiple
            filterable
            allow-create
            default-first-option
            :reserve-keyword="true"
            placeholder="逗号或回车成一个：如 量化、部署、投资"
          />
        </el-form-item>
      </el-form>
      <p v-if="tagsInvalid(draftTags)" class="banner banner--danger">{{ tagsHint(draftTags) }}</p>
      <p v-else class="tip">{{ tagsHint(draftTags) }}</p>
      <p class="tip">内容类型创建后不可改（要换类型就删掉重建）。链接只存 URL 与摘要，不抓取网页。标签只影响筛选，不参与内容判据（改标签不会把文档变成另一篇）。</p>
      <template #footer>
        <el-button :disabled="busyCreate" @click="creating = false">取消</el-button>
        <el-button
          type="primary"
          :loading="busyCreate"
          :disabled="!canCreate"
          @click="doCreate"
        >
          新增
        </el-button>
      </template>
    </el-dialog>

    <!-- 编辑：标题预填现值；正文**故意不回显**（读接口只给 LEFT(text,300) 的截断切片，
         拿它回显再保存就是把原文截断）—— 留空即只改标题，确实要改正文才粘贴完整全文。 -->
    <el-dialog
      v-model="editing"
      title="编辑知识库文档"
      width="620px"
      align-center
      :close-on-click-modal="false"
      :close-on-press-escape="!busyEdit"
      :show-close="!busyEdit"
      append-to-body
    >
      <el-form label-position="top">
        <el-form-item label="内容类型（创建后不可改）">
          <span class="fld-static">
            {{ typeLabel(editDoc) }}
            <span class="tag" :class="`tag--${typeOf(editDoc)}`">{{ typeOf(editDoc) }}</span>
          </span>
        </el-form-item>
        <el-form-item label="标题（必填）">
          <el-input v-model="editTitle" placeholder="标题" />
        </el-form-item>
        <!-- 链接卡片：改 URL 仍单块重存（后端拿库里 meta 里的原 URL 兜底，不会丢） -->
        <el-form-item v-if="typeOf(editDoc) === 'link'" label="链接地址（留空 = 不改）">
          <el-input v-model="editUrl" :placeholder="editUrlPlaceholder" />
        </el-form-item>
        <el-form-item :label="typeOf(editDoc) === 'link' ? '摘要（留空 = 不改）' : '正文（留空 = 只改标题）'">
          <el-input
            v-model="editContent"
            type="textarea"
            :rows="9"
            placeholder="要改正文就粘贴完整原文；留空只改标题。改正文时后端会删旧切片重切。"
          />
        </el-form-item>
        <!-- 标签：预填现值（列表接口投影出来了）。保存是**整体替换**：删一个 chip 再存就真删 -->
        <el-form-item label="标签">
          <el-select
            v-model="editTags"
            class="sel-full"
            multiple
            filterable
            allow-create
            default-first-option
            :reserve-keyword="true"
            placeholder="逗号或回车成一个"
          />
        </el-form-item>
      </el-form>
      <p v-if="tagsInvalid(editTags)" class="banner banner--danger">{{ tagsHint(editTags) }}</p>
      <p v-else class="tip">{{ tagsHint(editTags) }}</p>
      <p class="tip">
        正文不回显：列表接口只返回标题，正文只存在于切片里（且读接口每条只给前 300 字），
        拿截断的切片回显再保存会把原文截断。要改正文请粘贴完整全文。类型不可改 ——
        换了类型就等于换了篇内容（切块策略都跟着变），要换请删掉重建。
      </p>
      <template #footer>
        <el-button :disabled="busyEdit" @click="editing = false">取消</el-button>
        <el-button
          type="primary"
          :loading="busyEdit"
          :disabled="!editTitle.trim() || tagsInvalid(editTags)"
          @click="doEdit"
        >
          保存
        </el-button>
      </template>
    </el-dialog>

    <!-- 导入：JSONL 逐行 {title, content} → 预演（零写入）回显计数 → 手输 N 才实跑 -->
    <el-dialog
      v-model="importing"
      title="导入知识库文档（JSONL）"
      width="560px"
      align-center
      :close-on-click-modal="false"
      :close-on-press-escape="!busyPlan && !busyRun"
      :show-close="!busyPlan && !busyRun"
      append-to-body
    >
      <el-form label-position="top">
        <el-form-item>
          <span class="fld">
            每行一篇 {title, content, tags}
            <a class="tpl-link" href="#" @click.prevent="getTemplate">下载模板</a>
          </span>
          <el-input
            v-model="importText"
            type="textarea"
            :rows="7"
            placeholder='{"title": "文档标题", "content": "文档全文", "tags": ["量化", "部署"]}'
          />
        </el-form-item>
      </el-form>
      <p v-if="importParse.broken" class="banner banner--danger">
        有 {{ importParse.broken }} 行不是合法的 {"title", "content"}（缺标题/缺正文/不是 JSON），未计入下方条目 —— 不静默丢行，请修正后重试。
      </p>
      <p v-else-if="importParse.items.length" class="tip">
        已解析 {{ importParse.items.length }} 篇（其中 {{ importParse.items.filter((i) => i.tags.length).length }} 篇带标签），点「预演」后端会按内容去重并回显将新增多少篇；导入**只进手动域**。
      </p>
      <p v-if="importPlan" class="tip">
        预演结果：将新增 {{ importPlan.would_add }} 篇 · 已存在跳过 {{ importPlan.unchanged }} 篇 ·
        空标题或空正文 {{ importPlan.rejected_empty }} 篇（共 {{ importPlan.total }} 篇）。
      </p>
      <template #footer>
        <el-button :disabled="busyPlan || busyRun" @click="importing = false">取消</el-button>
        <el-button :loading="busyPlan" :disabled="busyRun || !importParse.items.length" @click="planImport">
          预演（不写入）
        </el-button>
        <el-button
          type="primary"
          :loading="busyRun"
          :disabled="busyPlan || !importPlan || !importPlan.would_add"
          @click="askImportRun"
        >
          实跑导入
        </el-button>
      </template>
    </el-dialog>

    <!-- 真删确认：比编辑重（物理删除），后果里点名备份、手输全文与域隔离 -->
    <ConfirmDialog
      v-model="deleting"
      title="彻底删除这篇知识库文档？"
      :consequences="deleteConsequences"
      :expect="deleteExpect"
      confirm-text="确认删除"
      :busy="busyDelete"
      @confirm="doDelete"
    />

    <!-- 导入实跑确认：expect = 预演给出的「将新增 N 篇」，与记忆导入同口径 -->
    <ConfirmDialog
      v-model="importRunAsk"
      title="按预演结果实跑导入？"
      :consequences="importConsequences"
      :expect="importExpect"
      confirm-text="确认导入"
      :busy="busyRun"
      tone="primary"
      @confirm="doImportRun"
    />

    <!-- 驳回理由：必填（后端同样 400 reason_required）。驳回不删正文，理由是日后
         重新审核的唯一线索 —— 所以这里不给「跳过」，只能取消。 -->
    <el-dialog
      v-model="rejectAsk"
      title="驳回这篇待审文档？"
      width="520px"
      align-center
      :close-on-click-modal="false"
      :close-on-press-escape="!busyReview"
      :show-close="!busyReview"
      append-to-body
    >
      <el-form label-position="top">
        <el-form-item label="驳回理由（必填）">
          <el-input
            v-model="rejectReason"
            type="textarea"
            :rows="4"
            placeholder="为什么这篇不能进知识库？（例如：与已有手册重复、来源不可信、没有出处）"
          />
        </el-form-item>
      </el-form>
      <p class="tip">
        驳回只是把它标成「已驳回」，正文不删（子代理随时能改好重提，你也能在面板里重新批准）。
        理由会记进该文档的审核台账。
      </p>
      <template #footer>
        <el-button :disabled="busyReview" @click="rejectAsk = false">取消</el-button>
        <el-button
          type="danger"
          :disabled="!rejectReason.trim()"
          @click="askRejectConfirm"
        >
          下一步：确认驳回
        </el-button>
      </template>
    </el-dialog>

    <!-- 审核动作二次确认：批准/驳回共用一个实例（与常驻候选审核同款，不造第三种交互） -->
    <ConfirmDialog
      v-model="reviewOpen"
      :title="reviewTitle"
      :consequences="reviewConsequences"
      :confirm-text="reviewConfirmText"
      :tone="reviewKind === 'reject' ? 'danger' : 'primary'"
      :busy="busyReview"
      @confirm="doReview"
    />
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import ItemCard from '../components/ItemCard.vue'
import PagerBar from '../components/PagerBar.vue'
import StatCard from '../components/StatCard.vue'
import http, { api, getToken } from '../api'
import { copyText } from '../utils/clipboard'
import { showError, showToast } from '../utils/dialog'
import { downloadTemplate } from '../utils/export'
import { toLocal } from '../utils/format'
import { hasAnyHit, isStale, mergeCounts } from '../utils/kb'

// 每页条数跟着各自 PagerBar 的下拉走 —— 两路各自独立：文档列表 / 切片命中 / 文档内切片
const pageSize = ref(20)
const hitSize = ref(20)
const chunkSize = ref(20)

const summary = ref(null)
const docs = ref([])
const docCount = ref(0)
const page = ref(1)
const hits = ref([])
const hitCount = ref(0)
const hitPage = ref(1)
const q = ref('')
/** 来源筛选：''=全部（默认视图不变）；否则原样透传后端 `source_type`（精确匹配，同导出端点口径） */
const sourceType = ref('')
/** 内容类型筛选：''=全部；否则透传后端 `content_type`（精确枚举匹配，后端同一份白名单校验） */
const contentType = ref('')
/** 各类型的文档数（后端 /kb/type-counts 给的真数字）。**前端绝不写死计数** ——
 *  老库未迁移该列时后端给 {}，Tab 上就不显示数字，而不是显示一个编出来的 0。 */
const typeCounts = ref({})
/** 标签筛选：tagRows = /kb/tags 给的 [{tag,count}]（**计数绝不写死**；空列表 ⇒ chip 行整行隐藏）；
 *  activeTag = 当前选中的那一个（再点一次取消）。与 q / source_type / content_type 取 AND。 */
const tagRows = ref([])
const activeTag = ref('')

/** 类型 Tab（顺序即展示顺序）。count 取不到就不渲染那个数 */
const TYPE_TABS = [
  { value: '', label: '全部', hint: '不按内容类型筛' },
  { value: 'skill', label: '技能', hint: '灌入器维护的技能文档（只读）' },
  { value: 'note', label: '手记', hint: '手记/粘贴正文' },
  { value: 'article', label: '文章', hint: '长文章' },
  { value: 'link', label: '链接', hint: '链接卡片（URL + 摘要，单块不切）' },
]

/** 新增弹框可选的类型：**不含 skill**（归灌入器）与 image（后端暂未开放 400） */
const CREATE_TYPES = [
  { value: 'note', label: '手记（正文切块）' },
  { value: 'article', label: '文章（正文切块）' },
  { value: 'link', label: '链接（URL + 摘要，单块）' },
]

const TYPE_LABEL = {
  skill: '技能', note: '手记', article: '文章', link: '链接', image: '图文',
}

/** 卡片/命中行的类型：🔴 字段缺席（旧库未迁移）时按 note 缺省，**不假装是技能** */
function typeOf(row) {
  return row?.content_type || 'note'
}

function typeLabel(row) {
  return TYPE_LABEL[typeOf(row)] || typeOf(row)
}

/** 卡上的标签 chip：字段缺席（老库未迁移）或空数组 ⇒ 空数组（当「没打标」，不编标签） */
function tagsOf(row) {
  return Array.isArray(row?.tags) ? row.tags : []
}

// 标签的硬上限：**抄后端 `TAG_MAX_ITEMS` / `TAG_MAX_LEN`**（kb.py 的 _clean_tags）。
// 前端只是提前把话说出来，判据仍在后端（前端能被绕过，前端不能是唯一那道闸）。
const TAG_MAX_ITEMS = 12
const TAG_MAX_LEN = 32

/** 标签即时提示：超限说清超在哪（最多 12 个 / 单项 ≤32 字），合规时只报口径 */
function tagsHint(list) {
  const arr = Array.isArray(list) ? list : []
  const long = arr.find((t) => String(t).trim().length > TAG_MAX_LEN)
  if (long) return `「${String(long).trim()}」超过 ${TAG_MAX_LEN} 字了，标签要能一眼看完`
  if (arr.length > TAG_MAX_ITEMS) return `已 ${arr.length} 个，最多 ${TAG_MAX_ITEMS} 个标签`
  return `逗号或回车成一个标签；最多 ${TAG_MAX_ITEMS} 个、单项 ≤${TAG_MAX_LEN} 字`
}

/** 超限 = 不让提交（与后端同一套判据，别让用户填完再吃 400） */
function tagsInvalid(list) {
  const arr = Array.isArray(list) ? list : []
  return arr.length > TAG_MAX_ITEMS || arr.some((t) => String(t).trim().length > TAG_MAX_LEN)
}

/** 空态那句话里说清「当前筛的是啥」 */
const filterLabel = computed(() => {
  const t = contentType.value ? `content_type=${contentType.value}` : ''
  const s = sourceType.value ? `source_type=${sourceType.value}` : ''
  const g = activeTag.value ? `tag=${activeTag.value}` : ''
  return [t, s, g].filter(Boolean).join(' · ')
})
const loading = ref(false)
const error = ref('')
/** 三分状态用：请求中（骨架屏）/ 成功且真的 0 条（空态）/ 失败（错误横幅）——「零文档」只在成功后出现 */
const docLoaded = ref(false)

const drawer = ref(false)
const curDoc = ref(null)
const chunks = ref([])
const chunkCount = ref(0)
const chunkPage = ref(1)
const chunkLoading = ref(false)

// ---- 写操作（批2：新增 / 编辑 / 真删 / 导入 / 导出），口径与记忆条目页一致 ----
const creating = ref(false)
const busyCreate = ref(false)
const draftTitle = ref('')
const draftContent = ref('')
const draftType = ref('note')
const draftUrl = ref('')
/** 新增弹框里的标签 chip 列表（逗号/回车由 el-select 自己变成一项） */
const draftTags = ref([])

const editing = ref(false)
const busyEdit = ref(false)
const editDoc = ref(null)
const editTitle = ref('')
const editContent = ref('')
const editUrl = ref('')
/** 编辑弹框里的标签 chip：预填现值，保存时**整体替换**（后端语义，不追加） */
const editTags = ref([])
/** 编辑弹框里的 URL 占位：列表接口不投影 meta（读不到原值），就明说「留空 = 不改」 */
const editUrlPlaceholder = computed(() => '留空则保留原链接；填 http(s) 开头的地址会替换')

const deleting = ref(false)
const deleteDoc = ref(null)
const busyDelete = ref(false)
const deleteExpect = computed(() => (deleteDoc.value ? deleteDoc.value.title : ''))
const deleteConsequences = computed(() =>
  deleteDoc.value
    ? [
        `真删《${deleteDoc.value.title}》：连同它的全部切片一起移除，检索与切片浏览立刻不再返回它，不可恢复。`,
        '删前自动备份：文档行与全部切片正文落在 console_ops 备份目录的 document.<id>.before.json（写不成则整次删除中止，这是唯一的回退路径）。',
        '只有手动文档能删：skill/skill_hk 由夜间灌入器维护，改动会被覆盖、被删的文档也会被清理扫掉，后端对它们直回 409。',
        '请照抄上面的标题全文确认 —— 一个字都不能差。',
      ]
    : [],
)

const importing = ref(false)
const importText = ref('')
const importParse = reactive({ items: [], broken: 0 })
const importPlan = ref(null)
const busyPlan = ref(false)
const busyRun = ref(false)
const importRunAsk = ref(false)
const importExpect = computed(() => (importPlan.value ? String(importPlan.value.would_add) : ''))
const importConsequences = computed(() =>
  importPlan.value
    ? [
        `将写入 ${importPlan.value.would_add} 篇新文档（已存在的 ${importPlan.value.unchanged} 篇会跳过，不覆盖旧文档）。`,
        '导入只进手动域：条目里带 source_type 会直接 422，不给调用方往 skill 域灌数据的路。',
        `请照抄「${importExpect.value}」确认 —— 这个数就是预演出来的将新增篇数，写进去一篇都不会多。`,
      ]
    : [],
)

const busyExport = ref(false)

// ---- 审核队列（批2）：子代理提交的知识文档先审后用 ----
/** 待审面板是否展开（chip 上点开/收起） */
const pendingOpen = ref(false)
/** 队列里的行（pending + rejected 一起给；pending_count 是 chip 上那个数） */
const pendRows = ref([])
const pendCount = ref(0)
const pendTotal = ref(0)
const pendPage = ref(1)
const pendSize = ref(20)
/** chip 文案三分支：红字待审 / 灰字历史 / 灰字空 —— 数据全来自 /kb/pending 的 count 与 pending_count */
const chipText = computed(() => {
  if (pendCount.value > 0) return `待审 ${pendCount.value}`
  if (pendTotal.value > 0) return `审核 · ${pendTotal.value} 条历史`
  return '审核 · 空'
})
const pendLoading = ref(false)
const pendError = ref('')
/** 批准/驳回共用一个确认框实例（与「常驻候选」审核同款） */
const reviewOpen = ref(false)
const reviewKind = ref('')
const reviewRow = ref(null)
const busyReview = ref(false)
/** 驳回理由：先在弹框里填（必填），再走 ConfirmDialog 二次确认 */
const rejectAsk = ref(false)
const rejectReason = ref('')

const reviewTitle = computed(() =>
  reviewKind.value === 'reject'
    ? '确认驳回这篇知识库文档？'
    : (reviewRow.value?.status === 'rejected' ? '重新批准已驳回的文档？' : '批准这篇知识库文档？'))
const reviewConfirmText = computed(() => (reviewKind.value === 'reject' ? '驳回' : '批准入库'))
const reviewConsequences = computed(() => {
  const row = reviewRow.value
  if (!row) return []
  if (reviewKind.value === 'reject') {
    return [
      `《${row.title}》标记为「已驳回」，立刻从待审队列沉底，并从列表/检索/导出里继续不可见。`,
      `驳回理由会记进审核台账：${rejectReason.value.trim()}`,
      '正文不删 —— 子代理可以改好重提，你也可以稍后在面板里重新批准。',
    ]
  }
  return [
    `《${row.title}》标记为已生效，按内容类型（${typeLabel(row)}）切块后落进 chunks。`,
    '此刻起它才会出现在下方文档列表、被 /kb/search 检索到、也进入导出文件。',
    '切块与状态改动在同一个事务里：要么都成，要么都不成。',
    row.dup_of ? `注意：库里已有一篇同名/近重复的《${row.dup_of_title}》（#${row.dup_of}），批准后两篇都会留在检索里。` : '',
  ].filter(Boolean)
})

/** 点 chip：开/收面板。展开时拉第一页（收起不清数据，再点开直接显示，不重复请求）。 */
function togglePending() {
  pendingOpen.value = !pendingOpen.value
  if (pendingOpen.value && !pendRows.value.length) loadPending()
}

/** 队列：计数全部来自后端（前端零写死）；出错清空，不拿上一次的旧队列冒充现状。 */
async function loadPending() {
  pendLoading.value = true
  try {
    const r = await api.kbPending({ page: pendPage.value, page_size: pendSize.value })
    pendRows.value = Array.isArray(r?.items) ? r.items : []
    pendTotal.value = Number(r?.count) || 0
    pendCount.value = Number(r?.pending_count) || 0
    pendError.value = ''
  } catch (e) {
    pendRows.value = []
    pendTotal.value = 0
    pendCount.value = 0
    pendError.value = `读取待审队列失败：${e.message}`
  } finally {
    pendLoading.value = false
  }
}

function onPendPage(p) {
  pendPage.value = p
  loadPending()
}

function onPendSize(s) {
  pendSize.value = Number(s)
  pendPage.value = 1
  loadPending()
}

function askApprove(row) {
  reviewRow.value = row
  reviewKind.value = 'approve'
  reviewOpen.value = true
}

/** 驳回第一步：先填理由（必填），填完才进二次确认 */
function openReject(row) {
  reviewRow.value = row
  reviewKind.value = 'reject'
  rejectReason.value = ''
  rejectAsk.value = true
}

function askRejectConfirm() {
  if (!rejectReason.value.trim()) return
  rejectAsk.value = false
  reviewOpen.value = true
}

async function doReview() {
  const row = reviewRow.value
  if (!row) return
  const reject = reviewKind.value === 'reject'
  busyReview.value = true
  try {
    if (reject) {
      await api.kbRejectDocument(row.doc_id, rejectReason.value.trim())
      showToast(`已驳回：${row.title}（正文保留，可再审）`)
    } else {
      const r = await api.kbApproveDocument(row.doc_id)
      showToast(`已批准：${row.title}（切成 ${r.chunks_count} 块切片，已进检索）`)
    }
    reviewOpen.value = false
    rejectReason.value = ''
    await loadPending()
    // 批准过的那篇已经进了主列表 ⇒ 计数、列表、标签 chip 一起刷
    refresh()
  } catch (e) {
    showError(`${reject ? '驳回' : '批准'}失败：${e.message}`)
  } finally {
    busyReview.value = false
  }
}

/**
 * 只有手动域（source_type='manual'）的文档能改/删 —— skill/skill_hk 域归灌入器，
 * 后端 _manual_only 409 兜底。
 *
 * 🔴 **判据严格**：字段缺席/值未知一律算**非手动域**（只读徽标、不渲染写按钮）。
 * 反过来（缺省当手动）会在读接口没投出 source_type 时给 skill 文档渲染出
 * 「编辑/删除」，看着能改、点下去才被 409 挡回 —— 保守方向才是安全方向。
 */
function isManual(doc) {
  return doc.source_type === 'manual'
}

// 防串：请求序号。发请求前 ++，回来时序号对不上 ⇒ 过期响应直接丢弃（utils/kb.js 的 isStale）。
// 不用 AbortController 是因为 axios 层不接 signal（api 客户端本轮只增不改），序号已足够防覆盖。
let reqSeq = 0

/** 检索框有词 ⇒ 检索态；没词 ⇒ 浏览文档列表（默认视图）。空词判定与 search() 里那处同义：都要 trim */
const searching = computed(() => !!q.value.trim())
/** 「任一有结果就算找得到」：文档命中或切片命中任一不为 0 就算数（两路都 0 才显示空态） */
const anyHit = computed(() => hasAnyHit(docCount.value, hitCount.value))

async function loadSummary() {
  try {
    summary.value = await api.kbSummary()
    error.value = ''
  } catch (e) {
    summary.value = null
    error.value = `知识库不可用：${e.message}（需要 KEEPSAKE_PG_DSN 指向只读实例）`
  }
}

/** 浏览态：全量文档列表（不带 q）—— 默认视图 / 清空后 / 未输词翻页都走这里 */
async function loadDocs() {
  const seq = ++reqSeq
  loading.value = true
  try {
    const data = await api.kbDocuments({ source_type: sourceType.value, content_type: contentType.value, tag: activeTag.value, page: page.value, page_size: pageSize.value })
    if (isStale(seq, reqSeq)) return
    docs.value = data.items
    docCount.value = data.count
    docLoaded.value = true
    error.value = ''
  } catch (e) {
    if (isStale(seq, reqSeq)) return
    docs.value = []
    docCount.value = 0
    error.value = `读取文档列表失败：${e.message}`
  } finally {
    if (!isStale(seq, reqSeq)) loading.value = false
  }
}

/**
 * 检索态：一个词两路并行 —— /kb/documents 按标题过滤下面的文档列表，
 * /kb/search 命切片正文。两路各拿各的 page/page_size，结果行再合成计数。
 * 序号不匹配（用户已经改词/清空了）⇒ 整个响应丢弃，绝不覆盖新结果。
 */
async function loadBoth(term) {
  const seq = ++reqSeq
  loading.value = true
  let docRes, hitRes
  try {
    [docRes, hitRes] = await Promise.all([
      api.kbDocuments({ q: term, source_type: sourceType.value, content_type: contentType.value, tag: activeTag.value, page: page.value, page_size: pageSize.value }),
      api.kbSearch({ q: term, page: hitPage.value, page_size: hitSize.value }),
    ])
  } catch (e) {
    if (isStale(seq, reqSeq)) return
    error.value = `知识库检索失败：${e.message}`
    loading.value = false
    return
  }
  if (isStale(seq, reqSeq)) return
  docs.value = docRes.items
  docCount.value = docRes.count
  hits.value = hitRes.items
  hitCount.value = hitRes.count
  docLoaded.value = true
  error.value = ''
  loading.value = false
}

/** 回车与点「搜索」按钮同一条路：有词两路重查、页码各回第 1 页；空词只回全量文档列表 */
async function search() {
  const term = q.value.trim()
  page.value = 1
  hitPage.value = 1
  // 空输入回车 ⇒ 只发 /kb/documents（不带 q、page=1）回全量列表，且**不发 /kb/search**（没词可搜）
  if (!term) return loadDocs()
  await loadBoth(term)
}

/** 清空 ✕：两路都清（切片清零），回到默认文档列表而不是空态 —— 与空输入回车同一条路 */
function onClear() {
  q.value = ''
  hits.value = []
  hitCount.value = 0
  return search()
}

/** 换来源：文档列表与检索两路都按新来源重查（页码各回第 1 页，否则第 5 页多半是空页） */
function onSource() {
  page.value = 1
  hitPage.value = 1
  return search()
}

/** 换内容类型：与 onSource 同一条路（Tab 选中即 GET /kb/documents?content_type=） */
function onType(v) {
  contentType.value = v
  page.value = 1
  hitPage.value = 1
  return search()
}

/** Tab 计数：老库未迁移 content_type 列时后端给 {} ⇒ 这里保持 {}，Tab 上不显示数字 */
async function loadTypeCounts() {
  try {
    const r = await api.kbTypeCounts()
    typeCounts.value = r?.counts || {}
  } catch (e) {
    typeCounts.value = {}
  }
}

/** 标签 chip 行：只有 /kb/tags 给了的标签才渲染（老库未迁移 ⇒ 空列表 ⇒ 整行隐藏）。
 *  出错时清空（不拿上一个库的旧标签冒充现状）。 */
async function loadTags() {
  try {
    const r = await api.kbTags()
    tagRows.value = Array.isArray(r?.items) ? r.items : []
  } catch (e) {
    tagRows.value = []
  }
}

/** 点 chip = 按该标筛，**再点同一个 = 取消**（页码各回第 1 页，与 onType/onSource 同款） */
function onTag(t) {
  activeTag.value = activeTag.value === t ? '' : t
  page.value = 1
  hitPage.value = 1
  return search()
}

function onPage(p) {
  page.value = p
  searching.value ? loadBoth(q.value.trim()) : loadDocs()
}
function onSize(s) {
  pageSize.value = Number(s)
  page.value = 1
  searching.value ? loadBoth(q.value.trim()) : loadDocs()
}
function onHitPage(p) {
  hitPage.value = p
  loadBoth(q.value.trim())
}
function onHitSize(s) {
  hitSize.value = Number(s)
  hitPage.value = 1
  loadBoth(q.value.trim())
}

async function openDoc(doc) {
  curDoc.value = doc
  chunks.value = []
  chunkCount.value = 0
  chunkPage.value = 1
  drawer.value = true
  await loadChunks()
}

async function loadChunks() {
  if (!curDoc.value) return
  chunkLoading.value = true
  try {
    const data = await api.kbDocumentChunks(curDoc.value.doc_id, {
      page: chunkPage.value,
      page_size: chunkSize.value,
    })
    chunks.value = data.items
    chunkCount.value = data.count
  } catch (e) {
    showError(`读取切片失败：${e.message}`)
  } finally {
    chunkLoading.value = false
  }
}

function onChunkPage(p) {
  chunkPage.value = p
  loadChunks()
}
function onChunkSize(s) {
  chunkSize.value = Number(s)
  chunkPage.value = 1
  loadChunks()
}

async function doCopy(id) {
  const ok = await copyText(id)
  ok ? showToast(`已复制 chunk_id：${id}`) : showError('复制失败，请手动选中复制')
}

/** 写操作之后刷新：计数与列表各走页面原有路径（检索态仍两路并行），防串序号照旧 */
function refresh() {
  loadSummary()
  loadTypeCounts()
  loadTags()
  // 待审 chip 的数字也要跟着走（审核动作本身已经单独拉过一次队列，这里只更新 chip）
  if (pendingOpen.value) loadPending()
  return searching.value ? loadBoth(q.value.trim()) : loadDocs()
}

// ---- 新增 ----
function openCreate() {
  draftTitle.value = ''
  draftContent.value = ''
  draftType.value = 'note'
  draftUrl.value = ''
  draftTags.value = []
  creating.value = true
}

/** 提交闸：标题必填；link 要 URL（摘要可空）；其余类型正文必填（口径与后端一致）；
 *  标签超限（>12 个 / 单项 >32 字）也不让提交 —— 与后端同一套判据，不让用户
 *  填完再吃一个 400。 */
const canCreate = computed(() => {
  if (!draftTitle.value.trim()) return false
  if (tagsInvalid(draftTags.value)) return false
  return draftType.value === 'link'
    ? !!draftUrl.value.trim()
    : !!draftContent.value.trim()
})

async function doCreate() {
  const title = draftTitle.value.trim()
  const content = draftContent.value.trim()
  if (!canCreate.value) return
  busyCreate.value = true
  try {
    const payload = { title, content, content_type: draftType.value }
    if (draftType.value === 'link') payload.url = draftUrl.value.trim()
    if (draftTags.value.length) payload.tags = [...draftTags.value]
    const r = await api.kbCreateDocument(payload)
    creating.value = false
    // 内容级幂等：同标题同内容提交后端判 unchanged（连 INSERT 都不发），别谎报新增
    showToast(r.status === 'unchanged'
      ? `已存在同名同内容的一篇，未重复新增：${title}`
      : `已新增：${title}（${typeLabel({ content_type: r.content_type })} · ${r.chunks_count} 块切片）`)
    refresh()
  } catch (e) {
    showError(`新增失败：${e.message}`)
  } finally {
    busyCreate.value = false
  }
}

// ---- 编辑 ----
/** 标题预填现值；正文不回显（切片读接口只给前 300 字，回显保存会把原文截断）。
 *  🔴 **类型不进表单**（创建后不可变，后端带异值 400），这里只把当前类型显示出来。
 *  🆕 标签**要**进表单（自由维度，可改）：预填现值，保存整体替换。 */
function openEdit(doc) {
  editDoc.value = doc
  editTitle.value = doc.title || ''
  editContent.value = ''
  editUrl.value = ''
  editTags.value = [...tagsOf(doc)]
  editing.value = true
}

async function doEdit() {
  const doc = editDoc.value
  const title = editTitle.value.trim()
  if (!doc || !title) return
  const content = editContent.value.trim()
  const url = editUrl.value.trim()
  busyEdit.value = true
  try {
    // 正文留空 = 只改标题（后端 title/content 至少给一个）；填了才改正文并同事务重切切片。
    // link 文档多一个可换的 url；**不带 content_type**（后端按文档自身类型重切）。
    const payload = content ? { title, content } : { title }
    if (typeOf(doc) === 'link' && url) payload.url = url
    // 🔴 **总是**带 tags（哪怕空数组）：后端语义是「给了就整体替换」，
    // 不带 = 不动 —— 那就删不掉标签了（用户摘掉的 chip 会自己长回来）。
    payload.tags = [...editTags.value]
    await api.kbUpdateDocument(doc.doc_id, payload)
    editing.value = false
    showToast(`已保存：${title}`)
    refresh()
  } catch (e) {
    showError(`保存失败：${e.message}`)
  } finally {
    busyEdit.value = false
  }
}

// ---- 真删 ----
function askDelete(doc) {
  deleteDoc.value = doc
  deleting.value = true
}

async function doDelete(typed) {
  const doc = deleteDoc.value
  if (!doc) return
  busyDelete.value = true
  try {
    await api.kbDeleteDocument(doc.doc_id, typed)
    deleting.value = false
    showToast(`已删除：${doc.title}`)
    refresh()
  } catch (e) {
    showError(`删除失败：${e.message}`)
  } finally {
    busyDelete.value = false
  }
}

// ---- 导入 ----
/**
 * JSONL 逐行解析成 items；坏行**计数返回**，绝不静默丢。
 * KB 条目必须有 title：缺标题/缺正文/不是 JSON 都算坏行（前端先挡一道，
 * 后端也会把空的计入 rejected_empty，但那时说不清是哪一种）。
 *
 * 🆕 `tags` 可选，与后端导出的行**同形**（数组，导出的 JSONL 原样改两处就能回灌）：
 * 手写时也接受逗号串（`"量化,部署"`）—— 前端顺手归一成数组，净化仍在后端
 * （前端不是唯一那道闸）。非字符串项直接丢掉，不把类型噪声送到后端。
 */
function parseJsonl(text) {
  const items = []
  let broken = 0
  for (const line of (text || '').split('\n')) {
    const s = line.trim()
    if (!s) continue
    try {
      const o = JSON.parse(s)
      if (o && typeof o.title === 'string' && o.title.trim()
          && typeof o.content === 'string' && o.content.trim()) {
        const raw = Array.isArray(o.tags) ? o.tags
          : (typeof o.tags === 'string' ? o.tags.split(/[,，]/) : [])
        items.push({
          title: o.title.trim(),
          content: o.content,
          tags: raw.filter((t) => typeof t === 'string').map((t) => t.trim()).filter(Boolean),
        })
      } else broken += 1
    } catch {
      broken += 1
    }
  }
  return { items, broken }
}

function openImport() {
  importText.value = ''
  importParse.items = []
  importParse.broken = 0
  importPlan.value = null
  importing.value = true
}

/**
 * 下载导入模板：走 fetch+blob（端点要 Authorization 头，裸 `<a href>` 带不了，
 * token 会漏进浏览器历史）。共用 utils/export.js，与记忆条目页同一套下载器。
 */
async function getTemplate() {
  try {
    await downloadTemplate('kb')
    showToast('已下载导入模板（keepsake-import-template-kb.jsonl）')
  } catch (e) {
    showError(`下载模板失败：${e.message}`)
  }
}

function currentItems() {
  const p = parseJsonl(importText.value)
  importParse.items = p.items
  importParse.broken = p.broken
  return p.items
}

/** 预演：后端零写入，只回显「将新增 / 已存在跳过 / 空标题正文」三个数 */
async function planImport() {
  busyPlan.value = true
  try {
    importPlan.value = await api.kbImportDocuments({ items: currentItems(), dry_run: true })
  } catch (e) {
    showError(`导入预演失败：${e.message}`)
  } finally {
    busyPlan.value = false
  }
}

function askImportRun() {
  if (!importPlan.value || !importPlan.value.would_add) return
  importRunAsk.value = true
}

async function doImportRun(typed) {
  busyRun.value = true
  try {
    const r = await api.kbImportDocuments({ items: importParse.items, dry_run: false, confirm: typed })
    importRunAsk.value = false
    importing.value = false
    showToast(`导入完成：新增 ${r.would_add} 篇 · 跳过 ${r.unchanged} 篇`)
    refresh()
  } catch (e) {
    showError(`导入失败：${e.message}`)
  } finally {
    busyRun.value = false
  }
}

// ---- 导出 ----
/** 失败时把后端的 JSON 文案取出来（否则只能看到「status 400」） */
async function exportError(res) {
  try {
    const d = JSON.parse(await res.text())
    return d?.detail?.message || d?.detail?.error || `后端返回 ${res.status}`
  } catch {
    return `后端返回 ${res.status}`
  }
}

/**
 * 导出：走 fetch 而不是裸 `<a href>` —— 端点要 Authorization 头，裸链子带不了
 * （token 只进请求头，绝不进 URL）。baseURL 取自 http 实例，不另写一份环境变量。
 * 后端导出只有 JSONL 一档；带来源筛选时按同一口径只导那一类（空=全量）。
 */
async function doExport() {
  busyExport.value = true
  try {
    const token = getToken()
    const qs = sourceType.value ? `?source_type=${encodeURIComponent(sourceType.value)}` : ''
    const res = await fetch(`${http.defaults.baseURL || ''}/api/v1/kb/export${qs}`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    })
    if (!res.ok) throw new Error(await exportError(res))
    const url = URL.createObjectURL(await res.blob())
    const a = document.createElement('a')
    a.href = url
    a.download = 'keepsake-kb-export.jsonl'
    document.body.appendChild(a)
    a.click()
    a.remove()
    setTimeout(() => URL.revokeObjectURL(url), 0)
    showToast(`导出已开始下载：${a.download}`)
  } catch (e) {
    showError(`导出失败：${e.message}`)
  } finally {
    busyExport.value = false
  }
}

onMounted(() => {
  loadSummary()
  loadTypeCounts()
  loadTags()
  // 待审 chip 的计数（面板关着也拉：chip 上那个「待审 N」不能等点开才知道）
  loadPending()
  loadDocs()
})
</script>

<style scoped>
.pad {
  padding: 20px;
}
/* 操作行：与 tokens 里 `.filter-result .acts` 同一份配方（flex + 8px 间距）——
   那条挂在 .filter-result 容器下，本页的操作行不是那个容器，故 scoped 复刻一份，数值不新创 */
.acts {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
}
/* 弹框里的说明行：与 DetailDrawer 的 .tip 同一份配方（12px/1.7 --text-faint）——
   scoped 样式跨组件不生效，只能各写一份，数值不许自创 */
.tip {
  margin: 12px 0 0;
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1.7;
}
.chunk-box {
  min-height: 120px;
}
.chunk-count {
  margin: 0 0 12px;
  color: var(--text-faint);
  font-size: 12.5px;
}
.chunk-list {
  margin: 0 0 16px;
  padding-left: 20px;
  color: var(--text);
  font-size: 13px;
}
.chunk-list li + li {
  margin-top: 14px;
  padding-top: 14px;
  border-top: 1px solid var(--border);
}
.chunk-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 6px;
}
.chunk-text {
  margin: 0;
  color: var(--text-dim);
  line-height: 1.7;
  white-space: pre-wrap;
}
/* 字段名旁的「下载模板」：与正文标题同一行，不另起一行抢视线 */
.fld {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 6px;
}
.tpl-link {
  color: var(--accent);
  font-size: 12px;
  text-decoration: none;
}
.tpl-link:hover {
  text-decoration: underline;
}

/* ---- 内容类型 Tab ----
   配方取 tokens 里现成的原子类数值（chip 底色 / 10px 控件圆角 / 描边色），
   不自创色值：--chip 底 + --border-2 描边，选中态换 --accent 描边与 --accent 字色。 */
.tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 16px;
}
.tab {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 12px;
  background: var(--chip);
  border: 1px solid var(--border-2);
  border-radius: var(--r-ctl);   /* 10 */
  color: var(--text-dim);
  font-size: 13px;
  line-height: 1.7;
  cursor: pointer;
}
.tab:hover {
  background: var(--panel-hover);
}
.tab--on {
  background: var(--accent-soft);
  border-color: var(--accent);
  color: var(--accent);
}
/* 计数：后端给的真实数字。老库未迁移 ⇒ v-if 不渲染，而不是显示一个编出来的 0 */
.tab-n {
  font-family: var(--mono);
  font-size: 12px;
  color: var(--text-faint);
}
.tab--on .tab-n {
  color: var(--accent);
}
/* 类型徽标的变体：技能=灰描边（同现有 tag，只压低存在感）；链接=描边带 ↗（点得进去） */
.tag--skill {
  opacity: 0.85;
}
.tag--link {
  border-color: var(--accent);
  color: var(--accent);
}
/* 弹框里的类型下拉：拉满一行（el-select 默认窄，放标题栏里会挤） */
.sel-full {
  width: 100%;
}
/* 编辑弹框里的静态字段（类型只读展示，不是可改控件） */
.fld-static {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--text-dim);
  font-size: 14px;
  line-height: 1.7;
}
</style>
