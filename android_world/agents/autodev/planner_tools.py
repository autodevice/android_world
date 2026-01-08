from typing import Optional


def tap(intent: str):
    """
    Declare an intent to tap a specific UI element on the current screen.
    You must give the executor enough context on what we're accomplishing.
    The executor will be able to make a series of taps to achieve our goal.

    This is a semantic instruction only — no coordinates should be provided.
    The executor will analyze the screenshot and determine the correct pixel
    location to perform the tap.

    Examples of `intent`:
      - "login button"
      - "search icon"
      - "cart tab"
      - "menu hamburger"
      - "the blue continue button at the bottom"

    Parameters:
        intent (str): A short natural-language description of the element
                      the agent should tap.
    """
    pass


def scan_for_element(intent: str):
    """
    When you're aware of the element, item, text, ID or button that we're looking for, trust the executor to
    scroll to find it itself. The executor performs well when it has goal oriented instructions.

    """


def gesture(intent: str):
    """
    Declare an intent to perform a gesture based on semantic direction
    or purpose. You must give the executor context on the goal, so they may perform actions
    towards the goal.

    The executor will translate the high-level intent into a concrete swipe
    vector using the screenshot.

    Examples of `intent`:
      - "swipe left on the "Do taxes" reminder and and delete it.
      - "swipe up to reveal the drawer from the bottom of the screen"
      - "swipe left to switch tab"
      - "swipe right to view next item"

    Parameters:
        intent (str): A natural-language description of the direction or
                      purpose of the swipe.
    """
    pass


def swipe_coords(start_x: int, start_y: int, end_x: int, end_y: int, intent: str = ""):
    """
    Perform a swipe gesture from exact start coordinates to exact end coordinates.
    This gives you precise control over the swipe path, which is especially useful for:
    - Scrolling specific distances
    - Swiping on specific UI elements
    - Horizontal scrolling in category rows
    - Precise navigation gestures

    IMPORTANT: Coordinates are in the logical screen space (scaled coordinates).
    The planner should analyze the screenshot and transcription to determine the exact
    coordinates of where to start and end the swipe.

    Examples:
      - Swipe from center-left to center-right: swipe_coords(100, 1200, 800, 1200, "scroll category row right")
      - Swipe down from top: swipe_coords(540, 200, 540, 1800, "scroll down to see more items")
      - Swipe up from bottom: swipe_coords(540, 2000, 540, 400, "scroll up to see previous items")

    Parameters:
        start_x (int): X coordinate where the swipe starts
        start_y (int): Y coordinate where the swipe starts
        end_x (int): X coordinate where the swipe ends
        end_y (int): Y coordinate where the swipe ends
        intent (str): Optional description of what this swipe is trying to accomplish
    """
    pass


def scroll(intent: str):
    """
    It is preferable that you call scan_for_element() tool call and let the executor make sense of what to do.

    If you do have to call scroll, you must give the executor context about what they're to look for.

    To scroll up, you would swipe down.
    You may ask the executor to scroll exactly one view port up or down if you're doing data extraction.

    The executor will translate the high-level intent into a concrete gesture to execute.

    Examples of `intent`:
      - "scroll down and find where all the expenses are"
      - "scroll right to see more properties."

    Parameters:
        intent (str): A natural-language description of the direction or
                      purpose of the scroll.
    """
    pass


def wait(seconds: int):
    """
    Declare an intent to wait for a specified duration before performing any
    further actions.

    This is useful when the UI is transitioning, animations are occurring,
    or network responses require time.

    Parameters:
        seconds (int): Number of seconds the agent should pause.
    """
    pass


def open_app(app_name: str):
    """
    Declare an intent to open an installed application on the device.

    The executor is responsible for locating the app icon on the home screen,
    app drawer, or search interface, and performing the necessary taps.

    Examples:
      - "YouTube"
      - "Gmail"
      - "Settings"

    Parameters:
        app_name (str): The exact name or closest recognizable name of the app.
    """
    pass


def go_back():
    """
    Declare an intent to navigate back to the previous screen.

    The executor will decide whether to press the system back button or
    another UI-specific back element, depending on the visible screen.
    """
    pass


def clear_text():
    """
    Declare an intent to clear the text in the currently focused input field.

    The executor will determine how to clear the text (e.g., selecting all and
    deleting, tapping the clear icon, or issuing repeated delete commands).
    """
    pass


def type_text(text: str, intent: str):
    """
    Declare an intent to type a specific string into a specific semantic field.

    The planner should describe *what* to type and *where*, and the executor
    determines the correct UI element and performs typing actions.

    Examples:
        text="hello", intent="search bar"
        text="john@example.com", intent="email field"

    Parameters:
        text (str): The exact text to be entered.
        intent (str): A natural-language description of the input field
                      (e.g., "username box", "search bar", "comment field").
    """
    pass


def answer(text: str):
    """
    Provide a natural-language answer or final statement to the user or system.

    This is not a UI action — it represents verbal or textual reasoning output
    from the agent (e.g., answering a question in the Android World task).

    The executor does not process this; instead, the agent returns it as the
    final answer for the environment to evaluate.

    Parameters:
        text (str): The answer the agent wants to output.
    """
    pass


def finish_task(success: bool):
    """
    Declare that the agent considers the task complete and wishes to terminate.

    If success=True, the agent believes it has achieved the required goal.
    If success=False, the agent is unable to complete the task.

    This will signal the environment to end the episode.

    Parameters:
        success (bool): Whether the agent believes the task was completed.
    """
    pass


def extract_data(extraction_instruction: str):
    """
    List and extract visible content from the current screen.
    
    Use this tool to list/transcribe visible content. The tool lists everything visible without filtering.
    YOU (the planner) handle filtering by criteria after getting the data.
    
    Examples of extraction_instruction:
      - "list all recipes on screen" - Lists all recipes visible with their details gievn
      - "list all items in file that can be seen on screen" - Lists all items visible in the file
      - "extract whole content of file" - Transcribes all text content visible in the open file
      - "list all activities on screen" - Lists all activities visible with their details
      - "list all tasks on screen" - Lists all tasks visible with their details
      - extract all text on screen of the current file
      - extract all text on screen of the current item
    
    The extraction agent will:
    - List ALL visible items/content matching the instruction
    - Include all visible details (names, descriptions, dates, categories, etc.)
    - Return structured data (JSON array, bullet list, or structured text)
    - NOT filter by criteria - it just lists what's visible
    
    After getting the data, YOU filter by criteria (date ranges, categories, prep time, etc.) in your logic.
    
    Parameters:
        extraction_instruction (str): Instruction on what to list/extract (e.g., "list all recipes on screen", "extract whole content of file")
    """
    pass


def get_ui_elements(focus: Optional[str] = None):
    """
    Get UI elements, buttons, icons, and interactive elements from the current screen.
    
    Use this tool when you need to find UI elements like buttons, icons, text fields, etc.
    This is focused on UI elements for navigation and interaction, NOT data extraction.
    
    Call this tool when:
    - You cannot find a button, icon, or UI element in the screenshot
    - You need to identify action bar buttons (rename, edit, delete, save icons)
    - You need to find navigation elements (tabs, menu items, back buttons)
    - You need to locate text fields or input elements
    - You need to find interactive elements (checkboxes, switches, sliders)
    
    The tool will return:
    - All buttons with their labels or icons
    - All icons (especially action bar icons, menu icons)
    - All text fields and input elements
    - All interactive elements
    - Their approximate locations and functions
    
    Examples of focus:
      - "action bar" - Focus on top action bar buttons/icons
      - "bottom navigation" - Focus on bottom navigation elements
      - "menu items" - Focus on menu or list items
      - "rename button" - Focus on finding rename/edit buttons
      - None - Get all UI elements on screen
    
    Parameters:
        focus (str, optional): Optional focus area to narrow down the search (e.g., "action bar", "bottom navigation", "rename button")
    """
    pass
