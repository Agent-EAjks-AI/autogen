import asyncio
import json
import base64
import uuid
import traceback
from datetime import datetime, timezone
from typing import Dict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from autogen_ext.tools.mcp._config import McpServerParams, StdioServerParams, SseServerParams, StreamableHttpServerParams
from loguru import logger
from mcp.types import (
    TextResourceContents, 
    BlobResourceContents, 
    ServerCapabilities,
    InitializeResult
)

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

from autogenstudio.mcp.client import (
    McpOperationError,
    McpConnectionError
)

def _extract_real_error(e: Exception) -> str:
    """Extract the real error message from potentially wrapped exceptions"""
    error_parts = []
    
    # Handle ExceptionGroup (Python 3.11+) - use getattr to avoid type checker issues
    if hasattr(e, 'exceptions') and getattr(e, 'exceptions', None):
        exceptions_list = getattr(e, 'exceptions')
        for sub_exc in exceptions_list:
            error_parts.append(f"{type(sub_exc).__name__}: {str(sub_exc)}")
    
    # Handle chained exceptions
    elif hasattr(e, '__cause__') and e.__cause__:
        current = e
        while current:
            error_parts.append(f"{type(current).__name__}: {str(current)}")
            current = getattr(current, '__cause__', None)
    
    # Handle context exceptions
    elif hasattr(e, '__context__') and e.__context__:
        error_parts.append(f"Context: {type(e.__context__).__name__}: {str(e.__context__)}")
        error_parts.append(f"Error: {type(e).__name__}: {str(e)}")
    
    # Default case
    else:
        error_parts.append(f"{type(e).__name__}: {str(e)}")
    
    return " | ".join(error_parts)

router = APIRouter()
active_sessions: Dict[str, Dict] = {}

class CreateWebSocketConnectionRequest(BaseModel):
    server_params: McpServerParams

async def send_websocket_message(websocket: WebSocket, message: dict):
    try:
        from fastapi.websockets import WebSocketState
        if websocket.client_state == WebSocketState.CONNECTED:
            await websocket.send_json(message)
    except Exception as e:
        real_error = _extract_real_error(e)
        logger.error(f"Error sending WebSocket message: {real_error}")

async def handle_mcp_operation(websocket: WebSocket, session: ClientSession, operation: dict):
    operation_type = operation.get("operation")
    
    try:
        if operation_type == "list_tools":
            result = await session.list_tools()
            tools_data = [tool.model_dump() for tool in result.tools]
            await send_websocket_message(websocket, {
                "type": "operation_result",
                "operation": "list_tools",
                "data": {"tools": tools_data},
                "timestamp": datetime.now(timezone.utc).isoformat()
            })
            
        elif operation_type == "call_tool":
            tool_name = operation.get("tool_name")
            arguments = operation.get("arguments", {})
            if not tool_name:
                raise McpOperationError("Tool name is required")
            
            result = await session.call_tool(tool_name, arguments)
            await send_websocket_message(websocket, {
                "type": "operation_result",
                "operation": "call_tool",
                "data": {"tool_name": tool_name, "result": result.model_dump()},
                "timestamp": datetime.now(timezone.utc).isoformat()
            })
            
        elif operation_type == "list_resources":
            result = await session.list_resources()
            
            await send_websocket_message(websocket, {
                "type": "operation_result",
                "operation": "list_resources",
                "data": result.model_dump(),
                "timestamp": datetime.now(timezone.utc).isoformat()
            })
            
        elif operation_type == "get_resource":
            uri = operation.get("uri")
            if not uri:
                raise McpOperationError("Resource URI is required")
            
            result = await session.read_resource(uri)
            
            await send_websocket_message(websocket, {
                "type": "operation_result",
                "operation": "read_resource",
                "data": result.model_dump(),
                "timestamp": datetime.now(timezone.utc).isoformat()
            })
            
        elif operation_type == "list_prompts":
            result = await session.list_prompts()
            prompts_data = [prompt.model_dump() for prompt in result.prompts]
            
            await send_websocket_message(websocket, {
                "type": "operation_result",
                "operation": "list_prompts",
                "data": {"prompts": prompts_data},
                "timestamp": datetime.now(timezone.utc).isoformat()
            })
            
        elif operation_type == "get_prompt":
            name = operation.get("name")
            arguments = operation.get("arguments")
            if not name:
                raise McpOperationError("Prompt name is required")
            
            result = await session.get_prompt(name, arguments)
            
            await send_websocket_message(websocket, {
                "type": "operation_result",
                "operation": "get_prompt",
                "data": result.model_dump(),
                "timestamp": datetime.now(timezone.utc).isoformat()
            })
        else:
            await send_websocket_message(websocket, {
                "type": "error",
                "error": f"Unknown operation: {operation_type}",
                "timestamp": datetime.now(timezone.utc).isoformat()
            })
            
    except Exception as e:
        real_error = _extract_real_error(e)
        logger.error(f"Error handling operation {operation_type}: {real_error}")
        await send_websocket_message(websocket, {
            "type": "error",
            "operation": operation_type,
            "error": real_error,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })

@router.websocket("/ws/{session_id}")
async def mcp_websocket(websocket: WebSocket, session_id: str):
    await websocket.accept()
    
    try:
        query_params = dict(websocket.query_params)
        server_params_encoded = query_params.get("server_params")
        
        if not server_params_encoded:
            await websocket.close(code=4000, reason="Missing server_params")
            return
        
        decoded_params = base64.b64decode(server_params_encoded).decode('utf-8')
        server_params_dict = json.loads(decoded_params)
        
        if server_params_dict.get("type") == "StdioServerParams":
            server_params_obj = StdioServerParams(**server_params_dict)
        elif server_params_dict.get("type") == "SseServerParams":
            server_params_obj = SseServerParams(**server_params_dict)
        elif server_params_dict.get("type") == "StreamableHttpServerParams":
            server_params_obj = StreamableHttpServerParams(**server_params_dict)
        else:
            await websocket.close(code=4000, reason="Invalid server parameters")
            return
        
        # Create the MCP client connections and session
        if isinstance(server_params_obj, StdioServerParams):
            stdio_params = StdioServerParameters(
                command=server_params_obj.command,
                args=server_params_obj.args,
                env=server_params_obj.env
            )
            async with stdio_client(stdio_params) as (read, write):
                async with ClientSession(read, write) as session:
                    await handle_mcp_session(websocket, session, session_id)
                    
        elif isinstance(server_params_obj, SseServerParams):
            async with sse_client(server_params_obj.url) as (read, write):
                async with ClientSession(read, write) as session:
                    await handle_mcp_session(websocket, session, session_id)
                    
        elif isinstance(server_params_obj, StreamableHttpServerParams):
            async with streamablehttp_client(server_params_obj.url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await handle_mcp_session(websocket, session, session_id)
        else:
            await websocket.close(code=4000, reason="Invalid server parameters")
            return
            
    except WebSocketDisconnect:
        pass  # Normal disconnection, no need to log
    except Exception as e:
        real_error = _extract_real_error(e)
        logger.error(f"MCP WebSocket error for session {session_id}: {real_error}")
        try:
            await send_websocket_message(websocket, {
                "type": "error",
                "error": f"Connection error: {real_error}",
                "timestamp": datetime.now(timezone.utc).isoformat()
            })
        except:
            pass
    finally:
        active_sessions.pop(session_id, None)


async def handle_mcp_session(websocket: WebSocket, session: ClientSession, session_id: str):
    try:
        # Initialize the MCP session
        initialize_result = await session.initialize()
        
        if initialize_result:
            capabilities = initialize_result.capabilities
        else:
            logger.warning(f"No initialize result for session {session_id}")
            capabilities = None
            
    except Exception as init_error:
        real_error = _extract_real_error(init_error)
        logger.error(f"Error during MCP initialization for session {session_id}: {real_error}")
        raise
    
    capabilities_data = capabilities.model_dump() if capabilities else None
    
    active_sessions[session_id] = {
        "created_at": datetime.now(timezone.utc),
        "last_activity": datetime.now(timezone.utc),
        "capabilities": capabilities_data
    }
    
    await send_websocket_message(websocket, {
        "type": "initialized",
        "session_id": session_id,
        "capabilities": capabilities_data,
        "timestamp": datetime.now(timezone.utc).isoformat()
    })
    
    # Main WebSocket message loop
    while True:
        try:
            raw_message = await websocket.receive_text()
            message = json.loads(raw_message)
            
            active_sessions[session_id]["last_activity"] = datetime.now(timezone.utc)
            
            message_type = message.get("type")
            
            if message_type == "operation":
                await handle_mcp_operation(websocket, session, message)
                
            elif message_type == "ping":
                await send_websocket_message(websocket, {
                    "type": "pong",
                    "timestamp": datetime.now(timezone.utc).isoformat()
                })
                
            else:
                await send_websocket_message(websocket, {
                    "type": "error",
                    "error": f"Unknown message type: {message_type}",
                    "timestamp": datetime.now(timezone.utc).isoformat()
                })
                
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON received from session {session_id}")
            await send_websocket_message(websocket, {
                "type": "error",
                "error": "Invalid message format",
                "timestamp": datetime.now(timezone.utc).isoformat()
            })

@router.post("/ws/connect")
async def create_mcp_websocket_connection(request: CreateWebSocketConnectionRequest):
    try:
        session_id = str(uuid.uuid4())
        
        server_params_json = json.dumps(request.server_params.model_dump())
        server_params_encoded = base64.b64encode(server_params_json.encode('utf-8')).decode('utf-8')
        
        return {
            "status": True,
            "message": "WebSocket connection URL created",
            "session_id": session_id,
            "websocket_url": f"/api/mcp/ws/{session_id}?server_params={server_params_encoded}",
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
    except Exception as e:
        real_error = _extract_real_error(e)
        logger.error(f"Error creating WebSocket connection: {real_error}")
        return {
            "status": False,
            "message": f"Failed to create WebSocket connection: {real_error}"
        }

@router.get("/ws/status/{session_id}")
async def get_mcp_session_status(session_id: str):
    session_info = active_sessions.get(session_id)
    
    if not session_info:
        return {
            "status": False,
            "message": "Session not found",
            "session_id": session_id
        }
    
    return {
        "status": True,
        "message": "Session active",
        "session_id": session_id,
        "connected": True,
        "capabilities": session_info.get("capabilities"),
        "created_at": session_info["created_at"].isoformat(),
        "last_activity": session_info["last_activity"].isoformat()
    }
