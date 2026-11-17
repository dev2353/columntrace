// Mirrors the lineage.json contract written by the Python extractor (schemaVersion 1).

export type Conf = 'parsed' | 'inferred' | 'unresolved'
export type NodeType = 'table' | 'notebook' | 'pipeline' | 'semantic_table' | 'unknown'
export type EdgeKind = 'reads' | 'writes' | 'calls' | 'binds' | 'references'
export type Liveness =
  | 'scheduled' | 'orchestrated' | 'called' | 'entry-point'
  | 'feeds-others' | 'dead-end' | 'uncalled'

export interface Evidence {
  file?: string
  line?: number
  via?: string
  snippet?: string
  activity?: string
  config?: string
  id?: number | string
}

export interface LNodeMeta {
  liveness?: Liveness
  lastChange?: string
  ageDays?: number
  stale?: boolean
  bulkCommit?: boolean
  commitSpread?: number
  family?: string
  familyMembers?: number
  newestInFamily?: boolean
  schedules?: { enabled: number; total: number }
  unqualified?: boolean
  external?: boolean
  configWarning?: string      // a config-table row for this table could not be read fully
  inLakehouse?: boolean       // found in the schema export (absent when there was no export)
  [k: string]: unknown
}

export interface LNode {
  id: string
  type: NodeType
  name: string
  path?: string | null
  itemId?: string | null
  meta: LNodeMeta
}

export interface LEdge {
  from: string
  to: string
  kind: EdgeKind
  confidence: Conf
  evidence?: Evidence[]
}

export interface ColumnEdge {
  from: string              // table id
  fromColumn: string        // '*' when a star could not be expanded
  to: string
  toColumn: string
  transform: 'direct' | 'expression' | 'aggregate' | 'star'
  confidence: 'parsed' | 'inferred'
  job: string               // notebook id
  expr?: string
  line?: number
}

export interface ColumnsData {
  schemaVersion: number
  tables: Record<string, string[]>   // table id -> column names (from the schema export)
  edges: ColumnEdge[]
  stats: Record<string, number>
}

/** A table-to-table flow known from one config row (not guessed from a job's inputs and outputs). */
export interface FlowRow {
  from: string
  to: string
  job: string                  // the loader notebook, or config:<table> when none was found
  confidence: Conf
  evidence?: Evidence
}

/** How current each input of an extract was (written by `extract` and `refresh`). */
export interface InputFile {
  source?: string
  table?: string
  branch?: string
  exportedAt?: string
  fetchedAt?: string
}
export interface LineageInputs {
  repo?: { branch?: string | null; commit: string; date: string }
  dates?: InputFile
  schema?: InputFile
  configTables?: InputFile[]
}

export interface Lineage {
  columns?: ColumnsData
  flows?: FlowRow[]
  meta: {
    schemaVersion: number
    generatedAt: string
    stats?: Record<string, unknown>
    annotations?: Record<string, unknown>
    inputs?: LineageInputs
  }
  nodes: LNode[]
  edges: LEdge[]
  warnings?: { item: string; message: string }[]
}

export const ALL_LIVENESS: Liveness[] = [
  'scheduled', 'orchestrated', 'called', 'entry-point', 'feeds-others', 'dead-end', 'uncalled',
]
export const ALL_TYPES: NodeType[] = ['table', 'notebook', 'pipeline', 'semantic_table', 'unknown']
export const ALL_CONF: Conf[] = ['parsed', 'inferred', 'unresolved']
