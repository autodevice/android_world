import json
import os
import time
from typing import Optional, Dict, Any

import cv2
import numpy as np
from dotenv import load_dotenv
from android_env.components import errors

from android_world.agents import base_agent
from android_world.agents.autodev import executor_tools
from android_world.agents.autodev.llm import AutoDevLLM, ToolCall
from android_world.agents.autodev.scratchpad import Scratchpad

# Import the logging system
from android_world.agents.autodev.logging_system import TestRunLogger, serialize_tool_call
from android_world.agents.autodev.prompts import (
    EXECUTOR_SYSTEM_PROMPT,
    PLANNER_SYSTEM_PROMPT,
)
from android_world.agents.autodev.transcription import transcribe_screen
from android_world.agents.autodev.util import (
    get_all_executor_tools_dict,
    get_all_planner_tools_dict,
    get_executor_registry,
    tool_call_to_query,
)
from android_world.env import interface
from android_world.env.json_action import JSONAction

# Load environment variables from .env file
load_dotenv()

MAX_EXECUTOR_STEPS = 10


def _get_task_difficulty(task_name: Optional[str]) -> Optional[str]:
    """Get task difficulty from task_metadata.json."""
    if not task_name:
        return None
    
    try:
        # Load task_metadata.json
        # Path: android_world/agents/autodev_agent.py -> android_world/task_metadata.json
        # __file__ = android_world/agents/autodev_agent.py
        # dirname(dirname(__file__)) = android_world/
        android_world_dir = os.path.dirname(os.path.dirname(__file__))
        metadata_path = os.path.join(android_world_dir, "task_metadata.json")
        
        if not os.path.exists(metadata_path):
            print(f"⚠️  Warning: task_metadata.json not found at {metadata_path}")
            return None
        
        with open(metadata_path, 'r') as f:
            tasks = json.load(f)
        
        # Find task by name
        for task in tasks:
            if task.get("task_name") == task_name:
                return task.get("difficulty")
        
        # Debug: print if task not found
        print(f"⚠️  Warning: Task '{task_name}' not found in task_metadata.json")
    except Exception as e:
        # Print the exception for debugging
        print(f"⚠️  Warning: Error loading task_metadata.json: {e}")
        import traceback
        traceback.print_exc()
        return None
    
    return None


def _get_planner_model(task_difficulty: Optional[str]) -> str:
    """Select planner model based on task difficulty.
    
    Args:
        task_difficulty: "easy", "medium", or "hard" from task_metadata.json
        
    Returns:
        Model name string for the planner LLM
    """
    if task_difficulty in ("easy", "medium"):
        # return "anthropic/claude-sonnet-4-5-20250929"
        return "anthropic/claude-haiku-4-5-20251001"
        # return "openai/gpt-5.2"
        # return "gemini/gemini-3-pro-preview"
    else:
        return "gemini/gemini-3-pro-preview"
        # return "anthropic/claude-haiku-4-5-20251001"
        # return "anthropic/claude-sonnet-4-5-20250929"


class AutoDev(base_agent.EnvironmentInteractingAgent):
    """Autodevice style agent."""

    def __init__(
        self,
        env: interface.AsyncEnv,
        name: str = "autodev",
        scale: float = 0.4,
        log_dir: str = "./test_runs",
        enable_logging: bool = True,
        task_name: Optional[str] = None,
    ):
        super().__init__(env, name)
        
        # Initialize logging first (needed for _log_print)
        self.enable_logging = enable_logging
        self.task_name = task_name
        # Pass env controller for ADB screenshot capture
        env_controller = env.controller if hasattr(env, 'controller') else None
        self.logger: TestRunLogger = TestRunLogger(log_dir, env_controller=env_controller) if enable_logging else None
        self.current_goal = None
        self.current_executor_session_id = None
        
        # Select planner model based on task difficulty
        task_difficulty = _get_task_difficulty(task_name)
        planner_model = _get_planner_model(task_difficulty)
        
        if task_difficulty:
            self._log_print(f"📋 Task difficulty: {task_difficulty} → Using planner: {planner_model}")
        else:
            self._log_print(f"📋 Task difficulty: unknown → Using planner: {planner_model} (default: Gemini)")
        
        self._step_count = 0
        self.scale = scale
        # Set the global scale in executor_tools
        executor_tools.SCALE = scale
        # Initialize shared scratchpad
        self.scratchpad = Scratchpad()
        self.planner_llm = AutoDevLLM(
            planner_model, PLANNER_SYSTEM_PROMPT, True, scratchpad=self.scratchpad
        )

        self.planner_tools_dict = get_all_planner_tools_dict()
        self.executor_tools_dict = get_all_executor_tools_dict()
        self.target_width = 0
        self.target_height = 0
        self.executor_registry = get_executor_registry()
        self._is_done = False

        # Navigation state tracking for deterministic behavior
        self.navigation_state = {
            "seen_items": set(),  # Track items/dates seen in transcriptions
            "scroll_history": [],  # Track scroll directions and what was visible
            "visited_screens": [],  # Track screens/navigation paths
            "last_visible_dates": [],  # Track dates visible in last transcription
            "scroll_direction": None,  # "up", "down", or None
            "scroll_count": 0,  # Count of scrolls in current search
        }
    
    def _log_print(self, message: str) -> None:
        """Print message and also log to file if logging is enabled."""
        print(message)
        # Safely check if logging is enabled and logger exists
        if (hasattr(self, 'enable_logging') and self.enable_logging and 
            hasattr(self, 'logger') and self.logger and 
            hasattr(self.logger, 'logs_file') and self.logger.logs_file):
            self.logger.log_to_file(message)

    def _resize_screenshot_to_logical_size(self, screenshot: np.ndarray) -> np.ndarray:
        """Resize screenshot to scaled logical screen size."""
        logical_width, logical_height = self.env.logical_screen_size
        # Scale down by self.scale for model input
        self.target_width = int(logical_width * self.scale)
        self.target_height = int(logical_height * self.scale)
        current_height, current_width = screenshot.shape[:2]

        if current_width != self.target_width or current_height != self.target_height:
            resized = cv2.resize(
                screenshot,
                (self.target_width, self.target_height),
                interpolation=cv2.INTER_LINEAR,
            )
            return resized
        return screenshot

    def _extract_visible_items_from_transcription(self, transcription: Optional[str]) -> Dict[str, Any]:
        """Extract visible items, dates, and content from transcription for state tracking."""
        if not transcription:
            return {"dates": [], "items": [], "text": ""}
        
        # Extract dates (various formats)
        import re
        dates = []
        date_patterns = [
            r'\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b',  # Day names
            r'\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d+',  # Month Day
            r'\b\d{1,2}/\d{1,2}/\d{2,4}\b',  # MM/DD/YYYY
            r'\bOct\s+\d+\b',  # Oct 13
        ]
        
        for pattern in date_patterns:
            matches = re.findall(pattern, transcription, re.IGNORECASE)
            dates.extend(matches)
        
        # Extract list items (lines that look like tasks/items)
        lines = transcription.split('\n')
        items = [line.strip() for line in lines if line.strip() and len(line.strip()) > 3]
        
        return {
            "dates": list(set(dates)),
            "items": items[:20],  # Limit to first 20 items
            "text": transcription[:500],  # First 500 chars for comparison
        }
    
    def _has_seen_content(self, transcription: Optional[str]) -> bool:
        """Check if we've seen this content before (to avoid revisiting)."""
        if not transcription:
            return False
        
        visible = self._extract_visible_items_from_transcription(transcription)
        
        # Check if transcription text hash was seen before (exact match)
        text_hash = hash(visible["text"])
        seen_hashes = self.navigation_state.get("seen_text_hashes", set())
        
        # Only return True if we've seen this EXACT hash before (not just similar content)
        if text_hash in seen_hashes:
            return True
        
        # For recipe/expense apps: check if we've seen the same items (first few items match)
        # This helps detect when we're stuck scrolling through the same content
        if visible["items"]:
            first_items = visible["items"][:5]  # First 5 items
            seen_items = self.navigation_state.get("seen_items", set())
            # If all first items were seen before, likely stuck
            if len(first_items) > 0 and all(item in seen_items for item in first_items):
                return True
        
        return False
    
    def _update_navigation_state(self, transcription: Optional[str], action: str):
        """Update navigation state after an action."""
        if not transcription:
            return
        
        visible = self._extract_visible_items_from_transcription(transcription)
        
        # Add dates to seen set
        for date in visible["dates"]:
            self.navigation_state["seen_items"].add(date)
        
        # Add items to seen set (for recipe/expense apps)
        for item in visible["items"][:10]:  # Track first 10 items
            if len(item) > 5:  # Only track meaningful items
                self.navigation_state["seen_items"].add(item)
        
        # Track scroll history
        if action.startswith("scroll"):
            self.navigation_state["scroll_history"].append({
                "direction": action,
                "visible_dates": visible["dates"],
                "visible_items_count": len(visible["items"]),
            })
            self.navigation_state["scroll_count"] += 1
        else:
            # Reset scroll count on non-scroll action
            self.navigation_state["scroll_count"] = 0
        
        # Track text hashes to detect revisiting (exact matches)
        if "seen_text_hashes" not in self.navigation_state:
            self.navigation_state["seen_text_hashes"] = set()
        text_hash = hash(visible["text"])
        self.navigation_state["seen_text_hashes"].add(text_hash)
    
    def _determine_scroll_strategy(self, target_date: Optional[str], transcription: Optional[str]) -> str:
        """Determine systematic scroll strategy based on target and current state."""
        if not transcription or not target_date:
            return "scroll_down"  # Default
        
        visible = self._extract_visible_items_from_transcription(transcription)
        visible_dates = visible["dates"]
        
        # If we've scrolled too much, try opposite direction
        if self.navigation_state["scroll_count"] >= 5:
            last_direction = self.navigation_state.get("scroll_direction", "down")
            return "scroll_up" if last_direction == "down" else "scroll_down"
        
        # If we've seen this content before, we're going in circles
        if self._has_seen_content(transcription):
            return "stop_scrolling"  # Signal to try alternative
        
        # Binary search strategy for dates:
        # If target is Oct 13 and we see "Oct 15", scroll down (older)
        # If target is Oct 13 and we see "Oct 10", scroll up (newer)
        # This requires date parsing logic (simplified here)
        
        # For now, use simple heuristic:
        # If we've been scrolling down and haven't found it, try up
        if self.navigation_state["scroll_count"] >= 3:
            if self.navigation_state.get("scroll_direction") == "down":
                return "scroll_up"
            else:
                return "scroll_down"
        
        return "scroll_down"  # Default

    def step(self, goal: str) -> base_agent.AgentInteractionResult:
        """
        This is the main agent loop.
        The android_world framework calls this in a loop until done = True or timeout/max_steps reached.
        """
        self._step_count += 1

        # Start new logging run on first step
        if self._step_count == 1 and self.enable_logging:
            self.current_goal = goal
            run_id = self.logger.start_new_run(
                goal=goal,
                task_name=self.task_name,
                scale=self.scale,
                logical_screen_size=self.env.logical_screen_size,
                planner_model=self.planner_llm.model,
                executor_model="anthropic/claude-sonnet-4-5-20250929",
                agent_name="autodev",
            )
            self._log_print(f"📝 Logging enabled. Run ID: {run_id}")

        state = self.get_post_transition_state()
        screenshot = self._resize_screenshot_to_logical_size(state.pixels.copy())

        # No automatic transcription - planner analyzes screenshot directly
        # Planner can call get_ui_elements() tool if it needs to find buttons/icons
        # Planner can call extract_data() tool if it needs to extract specific data
        
        # Track navigation state using screenshot hash (for scroll detection)
        screenshot_hash = hash(screenshot.tobytes())
        if screenshot_hash in self.navigation_state.get("seen_screenshots", set()):
            warning = "⚠️  CRITICAL: This screen was seen before - you are revisiting the same screen. STOP scrolling and try a different approach (tap on items, use search, or try alternative navigation)."
            self._log_print(warning)
        else:
            if "seen_screenshots" not in self.navigation_state:
                self.navigation_state["seen_screenshots"] = set()
            self.navigation_state["seen_screenshots"].add(screenshot_hash)
        
        # Add scroll count warning
        scroll_count = self.navigation_state.get("scroll_count", 0)
        if scroll_count >= 5:
            warning = f"⚠️  WARNING: You have scrolled {scroll_count} times. If you haven't found your target, STOP scrolling and try alternative strategies (tap items to view details, use search, or try different navigation)."
            self._log_print(warning)
        
        planned_step = self.planner_llm.chat(
            goal if self._step_count == 1 else None,
            screenshot,
            tools=self.planner_tools_dict,
            transcription=None,  # No automatic transcription
        )

        # Show simplified planner step info (not full JSON)
        if planned_step.get("tool_calls"):
            tool_names = []
            for tc in planned_step["tool_calls"]:
                if isinstance(tc, dict):
                    name = tc.get("function", {}).get("name")
                else:
                    tc_dict = serialize_tool_call(tc)
                    name = tc_dict.get("function", {}).get("name")
                # Skip todo updates in terminal output, only add valid names
                if name and name != "update_todos":
                    tool_names.append(name)
            if tool_names:
                self._log_print(f"Step {self._step_count}: Planning → {', '.join(tool_names)}")
            else:
                self._log_print(f"Step {self._step_count}: Planning → (updating todos)")
        else:
            self._log_print(f"Step {self._step_count}: Planning → (no actions)")

        # Log planner step
        if self.enable_logging:
            todo_state = None
            if hasattr(self.planner_llm, "todo_list"):
                todo_state = {
                    "todos": self.planner_llm.todo_list.todos
                    if hasattr(self.planner_llm.todo_list, "todos")
                    else []
                }

            # Ensure tool_calls is always a list, not None
            tool_calls = planned_step.get("tool_calls") or []

            self.logger.log_planner_step(
                step_number=self._step_count,
                user_input=goal if self._step_count == 1 else None,
                thinking=planned_step.get("content", ""),
                tool_calls=tool_calls,
                screenshot=screenshot,
                todo_list_state=todo_state,
            )

        # Safely handle tool_calls - ensure it's a list
        tool_calls_list = planned_step.get("tool_calls") or []
        if tool_calls_list:
            for tool_call in tool_calls_list:
                try:
                    if tool_call["function"]["name"] == "go_back":
                        self.env.execute_action(JSONAction(action_type="navigate_back"))
                        self.planner_llm.add_tool_result(
                            tool_call["id"], json.dumps({"success": True})
                        )
                    elif tool_call["function"]["name"] == "answer":
                        args = tool_call["function"]["arguments"]
                        if isinstance(args, str):
                            args = json.loads(args)

                        self.env.execute_action(
                            JSONAction(
                                action_type="answer",
                                text=args["text"],
                            )
                        )
                        self.planner_llm.add_tool_result(
                            tool_call["id"], json.dumps({"success": True})
                        )
                    elif tool_call["function"]["name"] == "finish_task":
                        # self.env.execute_action(JSONAction(action_type="finish_task"))
                        self._is_done = True
                        # CRITICAL: Must add tool result for Anthropic API
                        self.planner_llm.add_tool_result(
                            tool_call["id"], json.dumps({"success": True, "task_completed": True})
                        )
                        # Print planner cache statistics summary at end of conversation
                        if self.planner_llm.is_anthropic:
                            self._log_print("📊 Planner LLM Cache Stats:")
                            self.planner_llm.print_cache_summary()
                        if self.enable_logging:
                            # Note: success will be determined by task.is_successful() in the runner
                            self.logger.end_run(status="completed", success=None)
                    elif tool_call["function"]["name"] == "update_todos":
                        args = tool_call["function"]["arguments"]
                        if isinstance(args, str):
                            args = json.loads(args)

                        res = self.planner_llm.todo_list.update(args["todos"])
                        self._log_print(self.planner_llm.todo_list.pretty_print())
                        self.planner_llm.add_tool_result(tool_call["id"], json.dumps(res))
                    elif tool_call["function"]["name"] == "createItem":
                        args = tool_call["function"]["arguments"]
                        if isinstance(args, str):
                            args = json.loads(args)
                        
                        res = self.scratchpad.handle_create_args(args)
                        self._log_print(f"📝 Scratchpad: Created item '{res.get('key')}' - {res.get('title')}")
                        self.planner_llm.add_tool_result(tool_call["id"], json.dumps(res))
                    elif tool_call["function"]["name"] == "fetchItem":
                        args = tool_call["function"]["arguments"]
                        if isinstance(args, str):
                            args = json.loads(args)
                        
                        res = self.scratchpad.handle_fetch_args(args)
                        if res.get("success"):
                            self._log_print(f"📖 Scratchpad: Fetched item '{res.get('key')}' - {res.get('title')}")
                        else:
                            self._log_print(f"⚠️ Scratchpad: Key '{res.get('key')}' not found")
                        self.planner_llm.add_tool_result(tool_call["id"], json.dumps(res))
                    elif tool_call["function"]["name"] == "extract_data":
                        # Handle specialized data extraction using transcription agent
                        args = tool_call["function"]["arguments"]
                        if isinstance(args, str):
                            args = json.loads(args)
                        
                        extraction_instruction = args.get("extraction_instruction", "")
                        state = self.get_post_transition_state()
                        screenshot = self._resize_screenshot_to_logical_size(state.pixels.copy())
                        
                        from android_world.agents.autodev.transcription import extract_data_from_screen
                        extracted_data = extract_data_from_screen(screenshot, extraction_instruction)
                        
                        if extracted_data:
                            self._log_print(f"📊 Extracted data: {extracted_data[:200]}...")
                            self.planner_llm.add_tool_result(
                                tool_call["id"], 
                                json.dumps({"success": True, "extracted_data": extracted_data})
                            )
                        else:
                            self._log_print(f"⚠️ Data extraction failed or returned no data")
                            self.planner_llm.add_tool_result(
                                tool_call["id"], 
                                json.dumps({"success": False, "error": "Extraction failed or no matching data found", "extracted_data": ""})
                            )
                    elif tool_call["function"]["name"] == "get_ui_elements":
                        # Handle UI elements detection using transcription agent
                        args = tool_call["function"]["arguments"]
                        if isinstance(args, str):
                            args = json.loads(args)
                        
                        focus = args.get("focus")
                        state = self.get_post_transition_state()
                        screenshot = self._resize_screenshot_to_logical_size(state.pixels.copy())
                        
                        from android_world.agents.autodev.transcription import get_ui_elements
                        ui_elements = get_ui_elements(screenshot, focus)
                        
                        if ui_elements:
                            self._log_print(f"🔍 UI elements found: {ui_elements[:200]}...")
                            self.planner_llm.add_tool_result(
                                tool_call["id"], 
                                json.dumps({"success": True, "ui_elements": ui_elements})
                            )
                        else:
                            self._log_print(f"⚠️ UI elements detection failed")
                            self.planner_llm.add_tool_result(
                                tool_call["id"], 
                                json.dumps({"success": False, "error": "UI elements detection failed", "ui_elements": ""})
                            )
                    else:
                        self.execute_step(tool_call)
                except Exception as e:
                    # Log planner step failure
                    error_msg = f"Error executing planner tool {tool_call['function']['name']}: {str(e)}"
                    self._log_print(f"❌ PLANNER ERROR: {error_msg}")
                    if self.enable_logging:
                        # Update the last planner step with error status
                        if self.logger.planner_steps:
                            last_step = self.logger.planner_steps[-1]
                            if last_step.step_number == self._step_count:
                                last_step.status = "failed"
                                last_step.error_message = error_msg
                                self.logger._save_planner_steps()
                    # Always add tool result, even on error
                    self.planner_llm.add_tool_result(
                        tool_call["id"], json.dumps({"success": False, "error": error_msg})
                    )

        return base_agent.AgentInteractionResult(
            done=self._is_done,
            data={
                "agent_output": planned_step["content"],
                "step_count": self._step_count,
                "goal": goal,
                "error": False,
            },
        )

    def execute_step(self, planner_tool_call: ToolCall) -> None:
        """Run the executor loop for a single planner tool call and
        send the final result back to the planner via add_tool_result."""

        DIMENSIONS = f"""\n\n === DIMENSIONS ===
        The screenshot being sent is {self.target_width}x{self.target_height}
        it is {self.target_width} pixels in width.
        it is {self.target_height} pixels in height.
        The top left corner is 0,0.

        Please point to things given its current width and height.

        NEVER EXCEED width and height dimensions.

        """
        executor_llm = AutoDevLLM(
            "anthropic/claude-sonnet-4-5-20250929", EXECUTOR_SYSTEM_PROMPT + DIMENSIONS, scratchpad=self.scratchpad
        )

        # Start new executor session for logging
        if self.enable_logging:
            self.current_executor_session_id = self.logger.start_executor_session(
                f"planner_step_{self._step_count}",
                planner_tool_call_id=planner_tool_call["id"]
            )

        # This is the "intent" or high-level description from the planner.
        query = tool_call_to_query(planner_tool_call)

        # Track executor steps and results for logging
        executor_step_count = 0
        # Track all tool calls made during execution for report enhancement
        executor_tool_call_history = []

        for i in range(MAX_EXECUTOR_STEPS):
            executor_step_count += 1
            state = self.get_post_transition_state()
            screenshot = self._resize_screenshot_to_logical_size(state.pixels.copy())

            execution_step = executor_llm.chat(
                query if i == 0 else None,
                screenshot,
                tools=self.executor_tools_dict,
            )
            # Show simplified executor step info (not full JSON)
            if execution_step.get("tool_calls"):
                tool_names = []
                tool_details = []  # ADD THIS: Store tool name + coordinates
                for tc in execution_step["tool_calls"]:
                    if isinstance(tc, dict):
                        name = tc.get("function", {}).get("name")
                        args = tc.get("function", {}).get("arguments", {})
                    else:
                        name = getattr(tc, "function", None)
                        if name:
                            name = getattr(name, "name", None)
                            args = getattr(tc, "function", {}).get("arguments", {})
                    
                    if name:
                        # Parse args to extract coordinates
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except:
                                args = {}
                        
                        # Format tool call with coordinates
                        if name in ["click", "double_tap", "long_press"]:
                            x = args.get("x", "?")
                            y = args.get("y", "?")
                            tool_details.append(f"{name}({x}, {y})")
                            executor_tool_call_history.append(f"{name}({x}, {y})")
                        elif name == "swipe":
                            x0 = args.get("x0", "?")
                            y0 = args.get("y0", "?")
                            x1 = args.get("x1", "?")
                            y1 = args.get("y1", "?")
                            tool_details.append(f"{name}({x0},{y0}→{x1},{y1})")
                            executor_tool_call_history.append(f"{name}({x0},{y0}→{x1},{y1})")
                        elif name == "scroll":
                            direction = args.get("direction", "?")
                            x = args.get("x", "")
                            y = args.get("y", "")
                            coord_str = f" at ({x},{y})" if x and y else ""
                            tool_details.append(f"{name}({direction}{coord_str})")
                        else:
                            tool_details.append(name)
                            if name not in ["report", "extracted_data", "end"]:                           
                                executor_tool_call_history.append(name)
                
                if tool_details:
                    self._log_print(f"  Executor: {', '.join(tool_details)}")

            # Track tool results for logging
            tool_results = []

            if execution_step["tool_calls"]:
                for exec_call in execution_step["tool_calls"]:
                    fname = exec_call["function"]["name"]
                    args = exec_call["function"]["arguments"]
                    if isinstance(args, str):
                        args = json.loads(args)

                    # 1) Executor is done: return success back to planner
                    if fname == "report":
                        # Before executor reports, give it tool call history to write summary
                        if executor_tool_call_history and not hasattr(executor_llm, '_history_injected'):
                            # Inject tool call history into executor's context before it reports
                            history_message = f"""Before you finalize your report, here's a summary of all the tool calls you made during this subgoal:

Tool calls made: {', '.join(executor_tool_call_history)}

Please use this information to write a comprehensive summary in your report() notes. For example:
- If you tapped multiple times at the same location: "I tapped 4 times at coordinates (100,200) but there's no button or element there"
- If you scrolled multiple times: "I scrolled down 5 times but the transcription didn't change, reached the end of the list"
- If actions didn't work: "I tried tapping the rename button at (x,y) but nothing happened, the button might not be visible or labeled differently"

Now call report() again with your detailed summary including what you tried and what happened."""
                            
                            # Get current state for the history injection
                            history_state = self.get_post_transition_state()
                            history_screenshot = self._resize_screenshot_to_logical_size(history_state.pixels.copy())
                            
                            # Mark that we've injected history to avoid loops
                            executor_llm._history_injected = True
                            
                            # Inject history as a user message to executor
                            executor_llm.add_tool_result(exec_call["id"], "Review your tool call history and write a detailed summary in report()")
                            
                            # Make executor see the history before finalizing report
                            history_response = executor_llm.chat(
                                history_message,
                                history_screenshot,
                                tools=self.executor_tools_dict,
                            )
                            
                            # Check if executor called report() again with summary
                            if history_response.get("tool_calls"):
                                for tc in history_response["tool_calls"]:
                                    if isinstance(tc, dict):
                                        tc_name = tc.get("function", {}).get("name")
                                    else:
                                        tc_name = getattr(tc, "function", {}).get("name", "") if hasattr(tc, "function") else ""
                                    
                                    if tc_name == "report":
                                        # Executor called report() again with summary - use this one
                                        tc_args = tc.get("function", {}).get("arguments", {})
                                        if isinstance(tc_args, str):
                                            tc_args = json.loads(tc_args)
                                        args = tc_args  # Use the new report with summary
                                        break
                            
                            # Continue to process the report below
                        
                        # Executor has provided report (with or without history)
                        tool_results.append(
                            {"tool_call_id": exec_call["id"], "result": args}
                        )
                        
                        # Send executor's report directly to planner (no enhancement)
                        report_notes = args.get("notes", "") if isinstance(args, dict) else str(args)
                        
                        # Only log executor step when it completes and reports back
                        if self.enable_logging:
                            final_state = self.get_post_transition_state()
                            final_screenshot = self._resize_screenshot_to_logical_size(final_state.pixels.copy())
                            self.logger.log_executor_step(
                                session_id=self.current_executor_session_id,
                                step_number=executor_step_count,
                                query=query,  # Always use the original query from planner
                                thinking=execution_step.get("content", ""),
                                tool_calls=execution_step["tool_calls"],
                                tool_results=tool_results,
                                screenshot=final_screenshot,  # Save final screenshot
                                dimensions={
                                    "width": self.target_width,
                                    "height": self.target_height,
                                },
                                status="success",
                                planner_tool_call_id=planner_tool_call["id"],
                        )
                        self.planner_llm.add_tool_result(
                            planner_tool_call["id"],
                            json.dumps({"notes": report_notes}),
                        )
                        # Print executor cache stats at end of session
                        if executor_llm.is_anthropic:
                            self._log_print(f"📊 Executor Session Cache Stats:")
                            executor_llm.print_cache_summary()
                        return

                    # 2) Executor extracted data: return that back to planner
                    if fname == "extracted_data":
                        tool_results.append(
                            {"tool_call_id": exec_call["id"], "result": args}
                        )
                        # Only log executor step when it completes
                        if self.enable_logging:
                            final_state = self.get_post_transition_state()
                            final_screenshot = self._resize_screenshot_to_logical_size(final_state.pixels.copy())
                            self.logger.log_executor_step(
                                session_id=self.current_executor_session_id,
                                step_number=executor_step_count,
                                query=query,  # Always use the original query from planner
                                thinking=execution_step.get("content", ""),
                                tool_calls=execution_step["tool_calls"],
                                tool_results=tool_results,
                                screenshot=final_screenshot,
                                dimensions={
                                    "width": self.target_width,
                                    "height": self.target_height,
                                },
                                status="success",
                                planner_tool_call_id=planner_tool_call["id"],
                        )
                        self.planner_llm.add_tool_result(
                            planner_tool_call["id"],
                            json.dumps(args),
                        )
                        # Print executor cache stats at end of session
                        if executor_llm.is_anthropic:
                            self._log_print(f"📊 Executor Session Cache Stats:")
                            executor_llm.print_cache_summary()
                        return
                    
                    # 3) Scratchpad operations
                    if fname == "createItem":
                        res = self.scratchpad.handle_create_args(args)
                        self._log_print(f"📝 Executor Scratchpad: Created '{res.get('key')}' - {res.get('title')}")
                        executor_llm.add_tool_result(exec_call["id"], json.dumps(res))
                        tool_results.append(
                            {"tool_call_id": exec_call["id"], "result": json.dumps(res), "status": "success"}
                        )
                        # Track scratchpad operations in history
                        key = args.get("key", "?")
                        executor_tool_call_history.append(f"createItem({key})")
                        continue
                    
                    if fname == "fetchItem":
                        res = self.scratchpad.handle_fetch_args(args)
                        if res.get("success"):
                            self._log_print(f"📖 Executor Scratchpad: Fetched '{res.get('key')}' - {res.get('title')}")
                        else:
                            self._log_print(f"⚠️ Executor Scratchpad: Key '{res.get('key')}' not found")
                        executor_llm.add_tool_result(exec_call["id"], json.dumps(res))
                        tool_results.append(
                            {"tool_call_id": exec_call["id"], "result": json.dumps(res), "status": "success"}
                        )
                        # Track scratchpad operations in history
                        key = args.get("key", "?")
                        executor_tool_call_history.append(f"fetchItem({key})")
                        continue
                    
                    try:
                        json_action = self.executor_registry[fname](**args)
                        self._log_print(f"  {json_action}")
                        self.env.execute_action(json_action)
                        executor_llm.add_tool_result(exec_call["id"], "Done")
                        tool_results.append(
                            {"tool_call_id": exec_call["id"], "result": "Done", "status": "success"}
                        )
                        
                        # ADD THIS: Log EVERY step, not just completion/errors
                        if self.enable_logging:
                            current_state = self.get_post_transition_state()
                            current_screenshot = self._resize_screenshot_to_logical_size(current_state.pixels.copy())
                            self.logger.log_executor_step(
                                session_id=self.current_executor_session_id,
                                step_number=executor_step_count,
                                query=query,
                                thinking=execution_step.get("content", ""),
                                tool_calls=execution_step["tool_calls"],
                                tool_results=tool_results,
                                screenshot=current_screenshot,
                                dimensions={
                                    "width": self.target_width,
                                    "height": self.target_height,
                                },
                                status="success",
                                planner_tool_call_id=planner_tool_call["id"],
                            )
                    except errors.AdbControllerError as e:
                        error_str = str(e).lower()
                        if "device" in error_str and ("not found" in error_str or "offline" in error_str) or "unavailable" in error_str:
                            self._log_print("⚠️  Emulator disconnected. Attempting to reconnect...")
                            try:
                                self.env.controller.refresh_env()
                                time.sleep(2.0)
                                self._log_print("✓ Reconnected. Retrying action...")
                                json_action = self.executor_registry[fname](**args)
                                self.env.execute_action(json_action)
                                executor_llm.add_tool_result(exec_call["id"], "Done (after reconnection)")
                                tool_results.append(
                                    {"tool_call_id": exec_call["id"], "result": "Done (after reconnection)", "status": "success"}
                                )
                                if self.enable_logging:
                                    current_state = self.get_post_transition_state()
                                    current_screenshot = self._resize_screenshot_to_logical_size(current_state.pixels.copy())
                                    self.logger.log_executor_step(
                                        session_id=self.current_executor_session_id,
                                        step_number=executor_step_count,
                                        query=query,
                                        thinking=execution_step.get("content", ""),
                                        tool_calls=execution_step["tool_calls"],
                                        tool_results=tool_results,
                                        screenshot=current_screenshot,
                                        dimensions={
                                            "width": self.target_width,
                                            "height": self.target_height,
                                        },
                                        status="success",
                                        planner_tool_call_id=planner_tool_call["id"],
                                    )
                            except Exception as retry_error:
                                error_msg = f"Emulator disconnected and reconnection failed: {str(retry_error)}"
                                executor_llm.add_tool_result(exec_call["id"], error_msg)
                                tool_results.append(
                                    {"tool_call_id": exec_call["id"], "result": error_msg, "status": "failed"}
                                )
                                if self.enable_logging:
                                    try:
                                        error_state = self.get_post_transition_state()
                                        error_screenshot = self._resize_screenshot_to_logical_size(error_state.pixels.copy())
                                    except:
                                        error_screenshot = None
                                    self.logger.log_executor_step(
                                        session_id=self.current_executor_session_id,
                                        step_number=executor_step_count,
                                        query=query,
                                        thinking=execution_step.get("content", ""),
                                        tool_calls=execution_step["tool_calls"],
                                        tool_results=tool_results,
                                        screenshot=error_screenshot,
                                        dimensions={
                                            "width": self.target_width,
                                            "height": self.target_height,
                                        },
                                        status="failed",
                                        error_message=error_msg,
                                        planner_tool_call_id=planner_tool_call["id"],
                                    )
                                self.planner_llm.add_tool_result(
                                    planner_tool_call["id"],
                                    json.dumps({"status": "failed", "error": error_msg}),
                                )
                                # Print executor cache stats at end of session
                                if executor_llm.is_anthropic:
                                    self._log_print(f"📊 Executor Session Cache Stats:")
                                    executor_llm.print_cache_summary()
                                return
                        else:
                            raise
                    except Exception as e:
                        error_msg = f"Error executing {fname}: {str(e)}"
                        executor_llm.add_tool_result(exec_call["id"], error_msg)
                        tool_results.append(
                            {"tool_call_id": exec_call["id"], "result": error_msg, "status": "failed"}
                        )
                        if self.enable_logging:
                            error_state = self.get_post_transition_state()
                            error_screenshot = self._resize_screenshot_to_logical_size(error_state.pixels.copy())
                            self.logger.log_executor_step(
                                session_id=self.current_executor_session_id,
                                step_number=executor_step_count,
                                query=query,
                                thinking=execution_step.get("content", ""),
                                tool_calls=execution_step["tool_calls"],
                                tool_results=tool_results,
                                screenshot=error_screenshot,
                                dimensions={
                                    "width": self.target_width,
                                    "height": self.target_height,
                                },
                                status="failed",
                                error_message=error_msg,
                                planner_tool_call_id=planner_tool_call["id"],
                            )
                        self.planner_llm.add_tool_result(
                            planner_tool_call["id"],
                            json.dumps({"status": "failed", "error": error_msg}),
                        )
                        # Print executor cache stats at end of session
                        if executor_llm.is_anthropic:
                            self._log_print(f"📊 Executor Session Cache Stats:")
                            executor_llm.print_cache_summary()
                        return

            else:
                self._log_print(f"  Executor: ERROR - No tool call returned")

                # Log error state (this is a meaningful completion point)
                if self.enable_logging:
                    error_state = self.get_post_transition_state()
                    error_screenshot = self._resize_screenshot_to_logical_size(error_state.pixels.copy())
                    self.logger.log_executor_step(
                        session_id=self.current_executor_session_id,
                        step_number=executor_step_count,
                        query=query,  # Always use the original query from planner
                        thinking=execution_step.get("content", ""),
                        tool_calls=[],
                        tool_results=[{"error": "No tool call returned", "status": "failed"}],
                        screenshot=error_screenshot,
                        dimensions={
                            "width": self.target_width,
                            "height": self.target_height,
                        },
                        status="failed",
                        error_message="No tool call returned by executor LLM",
                        planner_tool_call_id=planner_tool_call["id"],
                    )

                self.planner_llm.add_tool_result(
                    planner_tool_call["id"],
                    json.dumps({"status": execution_step["content"]}),
                )
                # Print executor cache stats at end of session
                if executor_llm.is_anthropic:
                    self._log_print(f"📊 Executor Session Cache Stats:")
                    executor_llm.print_cache_summary()
                return
        
        # If we reach here, max executor steps were reached without completion
        self._log_print(f"❌ EXECUTOR ERROR: Max executor steps ({MAX_EXECUTOR_STEPS}) reached for query: {query}")
        
        # Collect all executor steps for summary
        all_steps_summary = []
        if self.enable_logging and self.current_executor_session_id:
            executor_steps = self.logger.executor_sessions.get(self.current_executor_session_id, [])
            for step in executor_steps:
                step_desc = f"Step {step.step_number}: "
                if step.tool_calls:
                    tool_names = []
                    for tc in step.tool_calls:
                        if isinstance(tc, dict):
                            name = tc.get("function", {}).get("name", "unknown")
                        else:
                            name = getattr(tc, "function", {}).get("name", "unknown") if hasattr(tc, "function") else "unknown"
                        tool_names.append(name)
                    step_desc += f"Tried tools: {', '.join(tool_names)}. "
                if step.thinking:
                    step_desc += f"Thinking: {step.thinking[:200]}"
                if step.error_message:
                    step_desc += f" Error: {step.error_message}"
                all_steps_summary.append(step_desc)
        
        # Get current screen state for summary
        try:
            current_state = self.get_post_transition_state()
            if current_state and current_state.pixels is not None:
                current_screenshot = self._resize_screenshot_to_logical_size(current_state.pixels.copy())
                current_transcription = transcribe_screen(current_screenshot)
            else:
                current_transcription = "No screenshot available"
        except Exception as e:
            self._log_print(f"⚠️  Error getting screen state for summary: {e}")
            current_transcription = "Error getting screen state"
        
        # Make final executor LLM call to summarize all attempts (NO TOOLS - text only)
        summary_prompt = f"""You have reached the maximum number of steps ({MAX_EXECUTOR_STEPS}) while trying to: {query}

You took the following steps:
{chr(10).join(all_steps_summary) if all_steps_summary else "No steps logged"}

Current screen transcription:
{current_transcription[:1000] if current_transcription else "No transcription available"}

Please provide a comprehensive summary of:
1. All the steps you took (what actions you performed)
2. What you tried to accomplish
3. What didn't work and why
4. What you observed on the screen
5. Any suggestions for alternative approaches

Do NOT make any tool calls - just provide a detailed text summary explaining everything you attempted and why it didn't work."""

        try:
            # Create a new executor LLM instance with NO TOOLS for summary
            summary_executor_llm = AutoDevLLM(
                "anthropic/claude-sonnet-4-5-20250929", 
                EXECUTOR_SYSTEM_PROMPT + DIMENSIONS, 
                scratchpad=self.scratchpad
            )
            summary_response = summary_executor_llm.chat(
                summary_prompt,
                screenshot=None,  # No screenshot needed for summary
                tools={},  # NO TOOLS - just text response
            )
            executor_summary = summary_response.get("content") or ""
            if not executor_summary:
                executor_summary = f"Executor took {executor_step_count} steps trying: {query}. All attempts failed."
        except Exception as summary_error:
            self._log_print(f"⚠️  Failed to generate executor summary: {summary_error}")
            executor_summary = f"Failed to generate summary. Executor took {executor_step_count} steps trying: {query}. All attempts failed."
        
        if self.enable_logging:
            error_state = self.get_post_transition_state()
            error_screenshot = self._resize_screenshot_to_logical_size(error_state.pixels.copy())
            self.logger.log_executor_step(
                session_id=self.current_executor_session_id,
                step_number=executor_step_count,
                query=query,
                thinking=f"Max executor steps reached. Summary: {executor_summary[:500]}",
                tool_calls=[],
                tool_results=[{"error": "Max executor steps reached", "status": "failed", "summary": executor_summary}],
                screenshot=error_screenshot,
                dimensions={
                    "width": self.target_width,
                    "height": self.target_height,
                },
                status="failed",
                error_message=f"Max executor steps ({MAX_EXECUTOR_STEPS}) reached",
                planner_tool_call_id=planner_tool_call["id"],
            )
        
        # Send comprehensive feedback to planner
        self.planner_llm.add_tool_result(
            planner_tool_call["id"],
            json.dumps({
                "status": "failed",
                "error": f"Max executor steps ({MAX_EXECUTOR_STEPS}) reached",
                "summary": executor_summary,
                "steps_taken": executor_step_count,
                "query": query
            }),
        )
        # Print executor cache stats at end of session
        if executor_llm.is_anthropic:
            self._log_print(f"📊 Executor Session Cache Stats:")
            executor_llm.print_cache_summary()

    def reset(self, go_home: bool = False) -> None:
        """Reset the agent."""
        super().reset(go_home)
        # Hide the coordinates/pointer visualization on screen
        self.env.hide_automation_ui()
        self._step_count = 0
        self.planner_llm = AutoDevLLM(
            "gemini/gemini-3-pro-preview", PLANNER_SYSTEM_PROMPT, True
        )
        self._is_done = False
        self._session_id = None  # Clear session ID on reset

        # End any active logging run
        if self.enable_logging and self.logger and self.logger.run_metadata:
            self.logger.end_run(status="reset", success=False, success_reason="Agent reset")

    def log_task_validation(self, task, env) -> float:
        """
        Check task validation and log debugging information.
        This should be called by the runner after the agent finishes.
        
        Args:
            task: The task object to validate
            env: The environment object
            
        Returns:
            The success score (0.0 to 1.0) from task.is_successful(env)
        """
        if not self.enable_logging or not self.logger:
            return task.is_successful(env)
        
        success_score = task.is_successful(env)
        self.logger.log_validation_debug(task, env, success_score)
        return success_score

    def on_error(self, error: Exception) -> None:
        """Handle errors during execution."""
        if self.enable_logging and self.logger:
            self.logger.end_run(
                status="failed",
                error_details=str(error),
                success=False,
                success_reason=f"Error: {str(error)}"
            )