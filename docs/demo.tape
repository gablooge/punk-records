# Terminal demo for the README. Record with https://github.com/charmbracelet/vhs:
#
#   pipx install punk-records   # or: pip install -e .  (so `punk` is on PATH)
#   vhs docs/demo.tape          # writes docs/demo.gif
#
# It runs in throwaway HOME and PUNK_HOME folders, so none of your real memories, paths or
# hostname appear in the recording.

Output docs/demo.gif

Set Shell "bash"
Set FontSize 18
Set Width 1100
Set Height 640
Set Padding 20
Set TypingSpeed 45ms

Hide
Type 'export HOME=$(mktemp -d) PUNK_HOME=$(mktemp -d)/store PS1="$ "'
Enter
Type 'mkdir -p ~/app && cd ~/app && git init -q && git remote add origin git@github.com:you/app.git'
Enter
Type 'echo "# App" > CLAUDE.md && echo "- run tests with make test" >> CLAUDE.md && ln -s CLAUDE.md AGENTS.md'
Enter
Type 'clear'
Enter
Show

Type 'punk init --machine demo'
Enter
Sleep 1.5s

Type 'punk track .'
Enter
Sleep 1.5s

Type 'punk save'
Enter
Sleep 2s

Type 'punk remember "- integration tests need docker running"'
Enter
Sleep 1.5s

Type 'cat CLAUDE.md'
Enter
Sleep 2s

Type 'punk context .'
Enter
Sleep 3s
