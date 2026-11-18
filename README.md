# 课文知识关系库

课文知识关系库用于处理本地合成教研文本，保存来源版本、文本片段和处理记录，供校内复核使用。

## 环境

需要 Python 3.11 或更新版本。应用运行不依赖第三方包；运行测试和构建前，安装固定版本的开发工具：`python -m pip install -r requirements-dev.txt`。

## 运行

执行 `PYTHONPATH=src python -m nlp_classroom_entity_relations.cli ingest fixtures/records.json` 导入演练记录，再执行 `PYTHONPATH=src python -m nlp_classroom_entity_relations.cli list` 查看当前文本处理结果。运行数据保存在 `var/records.json`。

## 数据模型

记录库只持久化两层输入，所有提及、实体、关系和卡片在读取时按当前输入重建：

- **记录与来源声明**：`TextRecord` 保存正文、来源、修订号、上下文，以及可选的
  `entities` / `mentions` / `relations` 声明。提及可以带字符位置或出现序号，
  关系可以带原文证据片段。
- **人工决定**：`decisions` 保存指代指派、实体合并、实体拆分和关系确认，
  并记录操作人 `actor` 与时间 `at`。`events` 保存追加式操作日志。

重建而非原地修补，保证纠正会传播：教师改正一处指代后，依赖该提及的关系卡片
会立即使用新人物，不会残留旧卡片。原文位置（`start` / `end` / `quote`）和人工
决定始终保留，修订后仍可追溯。

### 身份与歧义

- 提及按 `record_id + annotation_index` 稳定标识，人工指代决定跨修订保留；若同一
  位置的表层形式改变，旧决定不再套用到新文本，并标记 `assignment_stale`。
- 一个表层形式若匹配多个实体，全部作为候选保留，提及与关系保持 `ambiguous`，
  只有候选被合并为同一身份时才自动解析，绝不因姓名相同强行归并。
- 合并会把被合并实体标记为 `merged` 并保留 `merged_into`；拆分按每个提及自身的
  基础身份重新映射，因此不会把另一人物的关系继承过来。可以拆分当前合并目标，
  但必须留下至少一个实体，并可用 `target_entity_id` 指定新的合并目标。
- 关系确认绑定到具体修订与端点；修订或指代纠正后，卡片变为
  `needs_reconfirmation`，不会静默沿用旧的确认。

### 查询卡片

每张卡片包含 `text_version`（如 `lesson-c@1`）、`record_revision`、端点与其
候选、`evidence`（原文区间、引用句子、句段序号）、`mention_ids` 以及
`review_status`（`unreviewed` / `confirmed` / `needs_reconfirmation`）。
`card` 命令还会返回命中的完整句段，便于学生对照原文。

## 常用命令

```bash
# 导入与浏览
PYTHONPATH=src python -m nlp_classroom_entity_relations.cli ingest fixtures/records.json
PYTHONPATH=src python -m nlp_classroom_entity_relations.cli entities
PYTHONPATH=src python -m nlp_classroom_entity_relations.cli mentions --record classroom-entity-relations-lesson-c
PYTHONPATH=src python -m nlp_classroom_entity_relations.cli cards

# 按人物、谓词或复核状态检索卡片
PYTHONPATH=src python -m nlp_classroom_entity_relations.cli search --entity 李明
PYTHONPATH=src python -m nlp_classroom_entity_relations.cli search --review-status needs_reconfirmation

# 查看单张卡片的原文句段
PYTHONPATH=src python -m nlp_classroom_entity_relations.cli card 'classroom-entity-relations-lesson-c:r1@1'

# 纠正指代、合并、拆分与确认
PYTHONPATH=src python -m nlp_classroom_entity_relations.cli assign \
  'classroom-entity-relations-lesson-c:m4@1' teacher-wang --actor 王老师
PYTHONPATH=src python -m nlp_classroom_entity_relations.cli merge person-li-jia person-li-yi
PYTHONPATH=src python -m nlp_classroom_entity_relations.cli split person-li-jia person-li-yi
PYTHONPATH=src python -m nlp_classroom_entity_relations.cli confirm \
  'classroom-entity-relations-lesson-c:r1@1' --actor 王老师
PYTHONPATH=src python -m nlp_classroom_entity_relations.cli decisions
```

## 测试

执行 `python -m pytest -q` 检查文本规范化、句段定位、记录处理、指代纠正传播、
合并与拆分隔离、人工确认失效及本地持久化。
