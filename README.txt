MAGICAL PORTAL SYSTEM v1.7.0  -  UDP stream control for BrightSign (Windows 10/11)
==================================================================================

INSTALL / UPDATE
  1. Right-click the zip  >  Extract All.
  2. Double-click  Install.bat  (Windows asks once - click Open / Run).
  3. Answer the questions (Enter = default). Setup will:
       - detect an existing install and its version, and offer to update it
         (your portals and settings are always kept)
       - let you choose the install folder (default C:\MagicalPortal)
       - check for Python / FFmpeg / VLC, offer to install missing ones,
         and let you choose where Python is installed
       - create "Magical Portal System" shortcuts (Desktop + Start menu)
       - set up the address http://PortalSystem:8080 on this PC and the
         Windows Firewall rules (together, one admin prompt)
       - optionally start it at logon
       - list it in Settings > Apps > Installed apps with its version
  To update later: extract the new zip and run Install.bat again.

USE IT
  Start : "Magical Portal System" icon on the Desktop / Start menu (no black window).
  Tray  : while running, a portal icon sits in the hidden icons (^) next to the clock.
            left-click  = open the control page
            right-click = Start all / Stop all / Shut down
            hover       = how many portals are running
          It pops up a Windows notification if a portal loses its signal or stops.
  Stop  : right-click the tray icon > Shut down  (or the red button in the page).

  + New portal    name it, pick a source, set destination IP + port, Save, Start.
  Test source     Moving test card, HD colour bars, solid colour, your own picture
                  or your own video (looped). Switch it right on the card while live.
  Presets         480p / 720p / 1080p30 / 1080p60 / 4K30 fill in size, fps, bitrate, keyframes.
                  4K: BrightSign takes 4K H.264 up to 30 fps; use a graphics-card encoder.
  Audio channels  Mono, Stereo, 5.1, 7.1 or 16 ch (sent as 8 stereo tracks).
                  Test source: every channel has its own tone (ch1 300 Hz ... ch16 1800 Hz).
  Desktop capture Source type "Windows desktop capture" > Screen to capture:
                  All screens together / Screen 1 / Screen 2 / ... (Windows display numbers)
                  or One window (by title). A snapshot shows exactly what will be sent.
                  Works with any number of screens; the list is read live (use the refresh button
                  after plugging screens in/out). Optional mouse pointer.
                  Sound: pick a Windows recording device. For the PC's own sound use
                  "Stereo Mix" (Sound settings > Recording > Show disabled devices > Enable)
                  or a virtual audio cable (e.g. VB-Audio Cable). Mic / line-in work too.
  Relay           Source type "Network URL" = udp://@239.64.10.0:1234 -> sends it on to the
                  portal's destination. Video "Copy" = pure relay (almost no CPU), or encode to
                  change resolution/bitrate. Joins on your network adapter automatically and
                  reconnects by itself if the incoming stream drops.
  Change stream   type a new IP/port on the card and press Enter - restarts instantly.
  Watch mode      receive a UDP stream on this PC (VLC or FFplay) with live status.
  Test            listens 6 seconds and reports exactly what is arriving.
  Settings        FFmpeg/VLC paths, network adapter, notifications, firewall commands.

WEB PAGE ADDRESS  (Settings > Web page)
  Name     default PortalSystem  ->  http://PortalSystem:8080   (letters, numbers, -)
  Port     default 8080 (1024-65535). Changing it moves the page; streams keep running.
  Access   This PC only (default)  or  Other PCs / phones on the network too.
           From other devices:  http://PortalSystem:8080        (Windows PCs)
                                http://PortalSystem.local:8080  (phones, Macs, Windows)
                                http://<this PC's IP>:8080      (always works)
  Password optional, asked only from other devices - this PC never needs it.
  A new name needs one admin prompt on this PC ("Make the name work on this PC").

BRIGHTSIGN
  Multicast (e.g. 239.192.1.1:5000): every player set to udp://239.192.1.1:5000 plays it.
  Unicast (player IP, e.g. 192.168.1.50:5000): on that player use udp://0.0.0.0:5000
  Safe settings: H.264, 1920x1080, 30/60 fps, 6-15 Mbps, keyframe every 1-2 s, AAC 48 kHz.
  Multicast needs IGMP snooping AND an IGMP querier on the switch, otherwise players
  can take a minute or more to start.

ENCODERS (Encoder dropdown shows "works" / "not working here" for this PC)
  CPU (libx264)      always works; ~1-2 cores per 1080p stream.
  Intel Quick Sync   no fixed stream limit; needs a current Intel graphics driver.
  NVIDIA             GeForce: max 8 encodes per PC. The FFmpeg version must match the
                     NVIDIA driver - if FFmpeg says "minimum required Nvidia driver ...",
                     update the NVIDIA driver (or use Intel / CPU).
  Settings > Check encoders re-tests after a driver or FFmpeg update.
  A portal that fails 3 times right after starting stops retrying and shows why.

NO SIGNAL WHEN RECEIVING?
  1. Settings > Network adapter: pick the card on the video network.
  2. Firewall rules (Setup adds them, or copy them from Settings into Terminal (Admin)).
  3. Windows: that network's profile should be Private, not Public.

TROUBLESHOOTING (in the install folder)
  Start Magical Portal System.bat   runs it WITH a console window to see messages.
  logs\server.log                   messages when started from the icon.

UNINSTALL
  Settings > Apps > Installed apps > Magical Portal System > Uninstall
  or Uninstall.bat in the install folder.
