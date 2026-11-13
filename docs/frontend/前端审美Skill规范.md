---
name: omoi-aesthetic
description: >-
  The official frontend design and aesthetic system for Omoi: Geometric, Dynamic, Line-drawing, Pure White,
  Low-text-density SVG-driven interfaces, and multi-layered spatial depth. Use this skill whenever designing,
  building, auditing, or refactoring UI components, layouts, typography, and motion in Omoi.
---

# omoi-aesthetic · 几何线条艺术与自主动画生成体系

Omoi 项目专属前端艺术哲学与工程生成 Skill。
核心使命：**摆脱平庸死板的静态网页，将「几何作图、一笔画线条艺术、极简纯白留白、物理动态」确立为最高灵魂，并指导 AI 自主从零推演与创造全新的几何线条动画**。

---

## 1. 核心艺术公理 (The Core Axioms)

```
       [ 几何绘画 Drafting ] ── 尺规作图、建筑图纸感、数学秩序、参数刻度
               │
       [ 线条艺术 Line-Art ] ── 一笔画成、顺时针闭合、1px 纯矢量轮廓、形变蜕变
               │
       [ 简约艺术 Minimal ]  ── 留白作为引力场、以线代面、高反差纯白深墨、极低字密度
               │
       [ 物理动态 Dynamic ]  ── 抛物线重力掉落、扫线触碰级联、工程阻尼真实感
```

### 绝对红线与禁区 (Anti-Patterns)
1. **严禁粗暴弹现 (No Raw Fade-in)**：组件、卡片、头像绝不允许用无脑的透明度淡入（`opacity: 0 ➔ 1`），**必须通过线条在白纸上绘制出自身**；
2. **严禁泛黄做旧色调**：彻底摒弃做旧和纸、撕纸、朱砂印章等伪复古拟物化伪概念，全面拥抱纯白底色（`#FFFFFF`）；
3. **严禁文字拉伸抖动**：操作按钮严禁用长短不一的汉字切换（如 `保存` ➔ `保存中...` ➔ `已保存`），必须采用**固定尺寸纯 SVG 图标流转**；
4. **严禁汉字废话堆砌**：能用 SVG 绝不用汉字，必须外挂自制几何 Tips（`GeometricTooltip`）；
5. **严禁阻断式原生弹窗**：删除与修改必须行内直接触发或通过抽屉完成，严禁调用浏览器原生 `window.confirm`。

---

## 2. AI 自主开发新线条动画的五幕式法则 (The 5-Act Drafting Engine)

当 AI 面对一个全新的组件（如相框、播放器、进度条、折叠面板、下拉菜单、通知提示）时，**绝不要只写一个静态 div**，必须按照以下 **五幕式建筑图纸时序（The 5 Acts of Drafting）** 自主生成几何画线动效：

```
Act 1: 【定点与十字准星】── 工程基准点以微弹 (easeOutBack) 弹出，确立数学支点
Act 2: 【规尺引线与刻度】── 虚线引线拉出，针尖带出参数尺寸微标 (如 r=xx, w=xx)
Act 3: 【运笔画线生长】  ── 1.6px 墨黑细线沿几何路径顺时针高速生长闭合 (stroke-dashoffset)
Act 4: 【标尺消散与蜕变】── 辅助测量虚线平滑淡出，刚性直角平滑蜕变为柔性圆角
Act 5: 【着墨显真常驻】  ── 真实图像、文字或交互控件在几何结构内温润浮现
```

---

## 3. 自主创造新线条动画的六大母题算法 (6 Generative Archetypes)

AI 在设计新交互时，自主按以下 6 种数学母题推演并编写代码：

### 母题 A：极坐标尺规旋切 (Compass & Polar Sweep)
* **适用场景**：圆形头像、黑胶唱片、加载微指示器、仪表盘、圆形徽章。
* **数学与实现机制**：
  - 极轴旋转坐标：$x = x_c + r \cdot \sin(\theta)$，$y = y_c - r \cdot \cos(\theta)$，$\theta \in [0, 2\pi]$；
  - 针尖圆规沿圆周顺时针 360° 旋转，带领 `strokeDasharray={2 * Math.PI * r}` 闭合成圆；
  - 辅助半径引线（带尺寸刻度标签）以圆心为支点同步旋转，闭合瞬间与圆心黑点一同消散。

### 母题 B：游标卡尺矩形画线与形变 (Caliper Rect Trace & Morph)
* **适用场景**：卡片容器、输入框、按钮外框、代码块、弹层边框。
* **数学与实现机制**：
  - 一笔画矩形：从左上角出发，按“上 ➔ 右 ➔ 下 ➔ 左”顺时针生成路径（`getClockwiseRectPath`）；
  - 几何蜕变（Morphing）：线条闭合时，圆角半径 $R$ 从 $0$ 渐变为目标圆角（如 $24px$），直角矩形平滑蜕变为圆润胶囊。

### 母题 C：扫线物理触碰催化 (Sweep Line Catalyst)
* **适用场景**：顶部导航掉落、文章列表级联入场、页面全局开屏。
* **数学与实现机制**：
  - 掠过线条：一条带尾部虚化的半屏细线以 `easeOutQuad` 从屏幕左端横掠至右端；
  - 碰撞触媒（Collision Trigger）：线条头部坐标 $x_{line}(t)$ 掠过下方各元素中心时，依次激活对应子元素的物理级联下落或弹性展开。

### 母题 D：折叠撕边与几何展开 (Crease & Symmetrical Unfold)
* **适用场景**：抽屉滑出、自定义下拉菜单、折叠面板、文章信笺拆封。
* **数学与实现机制**：
  - 核心分割线从中心点向两侧对称生长延展；
  - 边框以分割线为基准向上下或左右如折纸般平滑展平展开，伴随柔和阴影漫反射。

### 母题 E：声波与脉冲几何震荡 (Waveform Oscilloscope)
* **适用场景**：BGM 音轨播放器、音频可视化、状态脉冲提示。
* **数学与实现机制**：
  - 1px 细线基于正弦驻波方程实时计算：$y(x, t) = A \cdot \sin(\omega x - \phi t) \cdot e^{-x^2 / 2\sigma^2}$；
  - 音频激活时细线产生有机震荡，静音时平滑回归为水平单线。

### 母题 F：视口溢出物理自由落体 (Edge Physics Falloff)
* **适用场景**：终端打字溢出、列表项删除滑落、拖拽脱落。
* **数学与实现机制**：
  - 溢出边缘生成物理粒子模型：
    $$\begin{cases} x_{t+1} = x_t + v_x \\ y_{t+1} = y_t + v_y \\ v_y \leftarrow v_y + g \quad (g = 0.40) \\ \text{rot}_{t+1} = \text{rot}_t + v_{rot} \\ \text{opacity}_{t+1} = \text{opacity}_t - 0.018 \end{cases}$$

---

## 4. 空间三层景深体系 (Spatial Depth Hierarchy)

拒绝扁平单页死板感，杜绝频繁页面跳转：

```
[ Layer 3: 顶层 (z-50) ] ── GeometricTooltip (自定义微气泡)
  └─ 纯白底、1px 黑框、font-mono text-[10px]、微三角指向箭头、悬浮 80ms 毫秒级淡入，彻底替换原生浏览器 title。

[ Layer 2: 浮层区 (z-30 ~ z-40) ] ── 悬停透视与原地抽屉
  ├─ ArticlePeekCard (z-40): 鼠标悬停标题 200ms 透出 16:9 封面、摘要诗句、标签徽章与字数。
  └─ ArticleMetaDrawer (z-30): 点击设置滑出 380px 纯白几何抽屉原地改，主视窗产生 4% 景深暗化与 backdrop-blur-[2px]。

[ Layer 1: 基底视窗 (z-10) ] ── 纯白无界专栏 / 几何数据栅格
  └─ 背景 #FFFFFF、行高 56px、1px 细线 border-[#E5E5E5]、留白透光。
```

---

## 5. 图标化与零文字抖动法则 (Icon-First & Low Text Density)

> **“能用 SVG 绝不用汉字废话；文字宽度变化是抖动的罪魁祸首。”**

* **尺寸恒定**：行级与顶栏操作按钮严控为 `w-7 h-7`（28px）或 `w-8 h-8`（32px）纯 SVG 容器；
* **纯图标流转**：
  - `idle`: `SaveDiskIcon`（`💾`）
  - `saving`: `SaveDiskIcon`（微脉冲）
  - `success`: `CheckMarkIcon`（`✓` 墨绿），800ms 后归位；
* **严禁动态文字伸缩**，零横向拉伸抖动。

---

## 6. 书写画布与图文排版标准 (Editorial Canvas)

* **专栏直出**：彻底砍掉“源码 / 渲染 / 双栏”多模式切换，直出单一纯白编辑专栏（`max-w-4xl` 居中）；
* **原生流式图文环绕**：
  - 段落组件采用 `contentEditable`，利用浏览器原生 Inline Flow 绕排浮动配图；
  - 靠左浮动：`float: left; margin: 0 1.5rem 1rem 0; width: ${width}%;`
  - 居中独占：`display: block; margin: 1.5rem auto; width: ${width}%; clear: both;`
  - 靠右浮动：`float: right; margin: 0 0 1rem 1.5rem; width: ${width}%;`
* **划词悬浮微控气泡条**：
  - 选中文本上方 8px 处毫秒级淡入纯白微胶囊（`BoldIcon`, `ItalicIcon`, `UnderlineIcon`, `CodeIcon`, `LinkIcon`, `QuoteIcon`, `HeadingIcon`）；
  - 数据流严格同步 `innerHTML` 与结构化 Block JSON，**严禁使用 `innerText` 破坏富文本标签**。

---

## 7. 纯白高反差设计字典 (Design Tokens)

```css
/* 画布与空间 */
--bg-canvas: #FFFFFF;         /* 纯白主画布 */
--bg-subtle: #FAFAFA;         /* 极浅悬停底色 */
--bg-drawer: #FFFFFF;         /* 浮层抽屉底色 */

/* 几何矢量线条 (1px) */
--line-subtle: #E5E5E5;       /* 浅灰界线 */
--line-contrast: #2B2A27;     /* 墨黑外框/标尺线 */

/* 墨印与字阶 */
--text-primary: #2B2A27;      /* 标题与重点墨色 (font-semibold) */
--text-secondary: #6E685E;    /* 正文次级灰 (font-normal) */
--text-muted: #8C857B;        /* 辅助微字与图标 (font-medium) */
--text-mono: #A39D91;         /* 等宽微标与参数 (font-mono text-[10px~11px]) */

/* 状态点缀 */
--status-active: #437A58;      /* 墨绿微标（已发布、保存成功对勾） */
--status-danger: #C25B4E;      /* 朱红微标（错误抖动、彻底销毁） */
```
