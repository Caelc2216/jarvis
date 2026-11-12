JARVIS - your voice assistant
=============================

SETTING UP (once)
  1. Unzip this folder somewhere you'll keep it, like Documents\Jarvis.
     (Don't run it from inside the zip.)
  2. Double-click  setup.bat  and answer the questions.
     If Windows says "Windows protected your PC", click "More info" then "Run anyway".
  That's it. Jarvis starts and will also start by itself when you turn on the PC.

USING IT
  Say "Hey Jarvis" (or clap twice). A glowing display pops up. Then just talk:
    - "Open Discord" / "open Minecraft" / "launch Fortnite"
    - "What's the weather in Chicago?"   "What time is it?"
    - "Set a 15 minute timer for pizza"
    - "Turn it up" / "pause" / "skip this song"
    - "Close Steam" / "minimize Chrome"
    - "Remember that my favorite team is the Broncos"
  When you're done, just stop talking (or say "goodbye") and it goes back to sleep.

TEACHING IT
  - If Jarvis doesn't know which app you mean, it asks, and remembers your answer.
  - "When I say the game, I mean Rocket League"  -> it remembers the nickname.
  - "Make a routine called gaming that opens Discord and Steam"
       -> from then on, just say "gaming".
  - Arrange those windows how you like and say "save this layout as gaming"
       -> it puts them back exactly there every time.
  - "Add Spotify to gaming" / "delete the gaming routine" / "what routines do I have?"

TEXT JARVIS FROM YOUR PHONE (optional)
  Double-click  Set up Discord.bat  and follow the steps (about 3 minutes).
  It makes a private Discord bot that only YOUR Discord account can use.
  Then message the bot from anywhere (your PC has to be on):
    - "open Steam so it can update"   "what's playing?"   "pause the music"
    - "set a timer for 20 minutes"     "close Discord"
    - "screenshot"  -> sends you a picture of your screens
  Jarvis also texts you when a timer finishes or there's an update.

BUTTONS
  Start Jarvis.bat   start it (if you stopped it)
  Stop Jarvis.bat    stop it
  setup.bat          change your answers (brain, voice, screen...)
  Update Jarvis.bat  get the newest version (or just say "update yourself")
  Set up Discord.bat text Jarvis from your phone

JARVIS DOESN'T HEAR YOU?
  Double-click  Test microphone.bat . It checks the Windows privacy switch
  (Settings > Privacy & security > Microphone > "Let desktop apps access your
  microphone" must be ON - Windows never asks for this), listens to you, and lets
  you pick the right microphone if it's using the wrong one.

IF SOMETHING'S WRONG
  Look at jarvis.log in this folder - it says what Jarvis heard and did.
  Your settings are in .env. Keep that file private; it can hold your keys.
