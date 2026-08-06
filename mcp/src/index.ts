#!/usr/bin/env node

import { McpServer } from '@modelcontextprotocol/server'
import { serveStdio } from '@modelcontextprotocol/server/stdio'
import * as z from 'zod/v4'

type JsonObject = Record<string, unknown>

const apiUrl = requiredEnv('IMMENSITY_API_URL').replace(/\/$/, '')
const projectId = requiredEnv('IMMENSITY_PROJECT_ID')
const accessToken = requiredEnv('IMMENSITY_PROJECT_TOKEN')

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
        const detail = typeof payload.detail === 'string' ? payload.detail : `Immensity API returned ${response.status}`
        throw new Error(detail)
    }
    return payload as T
}

function textResult(payload: unknown) {
    return {
        content: [{ type: 'text' as const, text: JSON.stringify(payload, null, 2) }],
    }
}

function createServer(): McpServer {
    const server = new McpServer(
        { name: 'immensity-mvp-build', version: '0.1.0' },
        {
            instructions: [
                'Use get_project_context before implementation to ground every change in the approved Problem Case and MVP solution.',
                'Use get_next_task to select dependency-ready work.',
                'Call update_task_progress when starting, blocking, or completing a task.',
                'Do not add market-fit, pricing, positioning, growth, or strategy scope.',
            ].join(' '),
        },
    )

    server.registerTool(
        'get_project_context',
        {
            title: 'Get project context',
            description: 'Return the evidence-backed Problem Case and complete 72-hour MVP solution for this project.',
            inputSchema: z.object({}),
        },
        async () => textResult(await api(`/ide/projects/${encodeURIComponent(projectId)}/context`)),
    )

    server.registerTool(
        'get_next_task',
        {
            title: 'Get next implementation task',
            description: 'Return the next incomplete task whose dependencies are complete.',
            inputSchema: z.object({}),
        },
        async () => textResult(await api(`/ide/projects/${encodeURIComponent(projectId)}/next-task`)),
    )

    server.registerTool(
        'update_task_progress',
        {
            title: 'Update task progress',
            description: 'Persist the status and optional implementation note for one solution task.',
            inputSchema: z.object({
                taskId: z.string().min(1).describe('Task ID from the MVP solution'),
                status: z.enum(['todo', 'in_progress', 'blocked', 'done']),
                note: z.string().max(2000).optional(),
            }),
        },
        async ({ taskId, status, note }) => textResult(await api(
            `/ide/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(taskId)}`,
            {
                method: 'PATCH',
                body: JSON.stringify({ status, note }),
            },
        )),
    )

    return server
}

void serveStdio(createServer)
console.error('Immensity MCP server running on stdio')
