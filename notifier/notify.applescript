-- AI 任务台账的通知小程序（由 build.sh 编译）。
-- 服务要弹通知时，先把「标题\n正文」写进 data/notify.txt 再打开本程序，它弹出通知后退出。
-- 你点通知时系统会再次打开本程序，这时没有待发内容，就打开看板。
on run
	set appPath to POSIX path of (path to me)
	set baseDir to do shell script "cd " & quoted form of appPath & "/../.. && pwd"
	set f to baseDir & "/data/notify.txt"
	set msg to ""
	try
		set msg to do shell script "cat " & quoted form of f & " && rm -f " & quoted form of f without altering line endings
	end try
	if msg is "" then
		set boardUrl to "http://127.0.0.1:47800"
		try
			set boardUrl to do shell script "cat " & quoted form of (baseDir & "/data/board_url")
		end try
		open location boardUrl
	else
		set AppleScript's text item delimiters to linefeed
		set t to text item 1 of msg
		set b to ""
		if (count of text items of msg) > 1 then set b to (text items 2 thru -1 of msg) as text
		display notification b with title t sound name "Glass"
		delay 1
	end if
end run
