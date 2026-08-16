#!/usr/bin/env node

import { McpServer } from '@modelcontextprotocol/server'
import { serveStdio } from '@modelcontextprotocol/server/stdio'
import * as z from 'zod/v4'

type JsonObject = Record<string, unknown>

const apiUrl = requiredEnv('IMMENSITY_API_URL').replace(/\/$/, '')
const projectId = requiredEnv('IMMENSITY_PROJECT_ID')
const accessToken = requiredEnv('IMMENSITY_PROJECT_TOKEN')
const projectPath = `/ide/projects/${encodeURIComponent(projectId)}`

function requiredEnv(name: string): string {
    const value = process.env[name]?.trim()
    if (!value) throw new Error(`${name} is required`)
    return value
}

async function api<T extends JsonObject>(path: string, init?: RequestInit): Promise<T> {
    const response = await fetch(`${apiUrl}${path}`, {
        ...init,
        headers: {
            Authorization: `Bearer ${accessToken}`,
            'Content-Type': 'application/json',
            ...(init?.headers ?? {}),
        },
    })
    const payload = await response.json().catch(() => ({})) as JsonObject
    if (!response.ok) {
        const detail = typeof payload.detail === 'string'
            ? payload.detail
            : `Immensity API returned ${response.status}`
        throw new Error(detail)
    }
    return payload as T
}

function toolResult(payload: JsonObject) {
    return {
        content: [{ type: 'text' as const, text: JSON.stringify(payload, null, 2) }],
        structuredContent: payload,
    }
}

const readOnly = {
    readOnlyHint: true,
    destructiveHint: false,
    idempotentHint: true,
    openWorldHint: false,
}

const writesProgress = {
    readOnlyHint: false,
    destructiveHint: false,
    idempotentHint: true,
    openWorldHint: false,
}

function createServer(): McpServer {
    const server = new McpServer(
        { name: 'immensity-build', version: '0.2.0' },
        {
            instructions: [
                'Start with get_build_contract, then fetch only the documents needed for the current task.',
                'Use get_next_task and respect dependencies.',
                'Report blockers instead of changing approved scope.',
                'Submit verification before marking work done; never claim an unrun check passed.',
                'Do not add pricing, growth, positioning, or unrelated product behavior.',
            ].join(' '),
        },
    )

    server.registerTool(
        'get_build_contract',
        {
            title: 'Get approved Build Contract',
            description: 'Return the approved Build Contract and its exact immutable version. Call this first.',
            inputSchema: z.object({}),
            annotations: readOnly,
        },
        async () => toolResult(await api(`${projectPath}/documents/build_contract`)),
    )

    server.registerTool(
        'list_build_documents',
        {
            title: 'List Build documents',
            description: 'List the focused Build package without loading every document into context.',
            inputSchema: z.object({}),
            annotations: readOnly,
        },
        async () => toolResult(await api(`${projectPath}/documents`)),
    )

    server.registerTool(
        'get_build_document',
        {
            title: 'Get one Build document',
            description: 'Fetch one approved Build document by key to keep context focused.',
            inputSchema: z.object({
                key: z.enum([
                    'features',
                    'requirements',
                    'tasks',
                    'test_plan',
                    'decisions',
                    'agents',
                    'claude',
                    'copilot_instructions',
                ]),
            }),
            annotations: readOnly,
        },
        async ({ key }) => toolResult(await api(`${projectPath}/documents/${key}`)),
    )

    server.registerTool(
        'get_project_status',
        {
            title: 'Get Build status',
            description: 'Return the approved version, task counts, completion state, and active blockers.',
            inputSchema: z.object({}),
            annotations: readOnly,
        },
        async () => toolResult(await api(`${projectPath}/status`)),
    )

    server.registerTool(
        'get_next_task',
        {
            title: 'Get next implementation task',
            description: 'Return the next todo task whose declared dependencies are complete.',
            inputSchema: z.object({}),
            annotations: readOnly,
        },
        async () => toolResult(await api(`${projectPath}/next-task`)),
    )

    server.registerTool(
        'update_task_progress',
        {
            title: 'Update task progress',
            description: 'Move a task to todo or in progress and attach a concise implementation note.',
            inputSchema: z.object({
                taskId: z.string().min(1).describe('Stable task ID from TASKS.md'),
                status: z.enum(['todo', 'in_progress']),
                note: z.string().max(2000).optional(),
            }),
            annotations: writesProgress,
        },
        async ({ taskId, status, note }) => toolResult(await api(
            `${projectPath}/tasks/${encodeURIComponent(taskId)}`,
            { method: 'PATCH', body: JSON.stringify({ status, note }) },
        )),
    )

    server.registerTool(
        'report_blocker',
        {
            title: 'Report task blocker',
            description: 'Mark a task blocked with the concrete reason; do not silently change scope.',
            inputSchema: z.object({
                taskId: z.string().min(1),
                note: z.string().min(1).max(2000),
            }),
            annotations: writesProgress,
        },
        async ({ taskId, note }) => toolResult(await api(
            `${projectPath}/tasks/${encodeURIComponent(taskId)}`,
            { method: 'PATCH', body: JSON.stringify({ status: 'blocked', note }) },
        )),
    )

    server.registerTool(
        'submit_verification',
        {
            title: 'Verify and complete task',
            description: 'Mark a task done only with an honest summary and the checks actually run.',
            inputSchema: z.object({
                taskId: z.string().min(1),
                summary: z.string().min(1).max(2000),
                checks: z.array(z.object({
                    command: z.string().min(1).max(500),
                    outcome: z.literal('passed').describe('Completion accepts only checks that passed'),
                    details: z.string().max(2000).optional(),
                })).min(1).max(20),
            }),
            annotations: writesProgress,
        },
        async ({ taskId, summary, checks }) => toolResult(await api(
            `${projectPath}/tasks/${encodeURIComponent(taskId)}`,
            {
                method: 'PATCH',
                body: JSON.stringify({
                    status: 'done',
                    note: summary,
                    verification: { summary, checks },
                }),
            },
        )),
    )

    server.registerTool(
        'get_project_context',
        {
            title: 'Get full project context',
            description: 'Compatibility tool returning the Problem Case and complete approved Build data. Prefer focused document tools.',
            inputSchema: z.object({}),
            annotations: readOnly,
        },
        async () => toolResult(await api(`${projectPath}/context`)),
    )

    return server
}

void serveStdio(createServer)
console.error('Immensity Build MCP server running on stdio')
