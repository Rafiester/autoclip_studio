STYLESHEET = """
* { font-family: "Segoe UI"; font-size: 13px; color: #e6e9ef; }
QMainWindow, QWidget { background: #0c0f14; }
QFrame#sidebar { background: #11151d; border-right: 1px solid #242a35; }
QLabel#brand { font-size: 22px; font-weight: 700; color: #ffffff; padding: 12px 10px; }
QLabel#pageTitle { font-size: 28px; font-weight: 700; color: #ffffff; }
QLabel#muted { color: #8993a4; }
QLabel#metricValue { font-size: 22px; font-weight: 700; color: #ffffff; }
QFrame#card, QGroupBox { background: #151a23; border: 1px solid #262d39; border-radius: 12px; }
QGroupBox { margin-top: 14px; padding: 18px 14px 14px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 0 6px; color: #b8c0cc; }
QPushButton { background: #202631; border: 1px solid #313947; border-radius: 8px; padding: 9px 14px; }
QPushButton:hover { background: #29313e; border-color: #435066; }
QPushButton:pressed { background: #171c24; }
QPushButton:disabled { color: #626b79; background: #171b22; border-color: #222832; }
QPushButton#primary { background: #6d5dfc; border-color: #796aff; color: white; font-weight: 700; padding: 11px 18px; }
QPushButton#primary:hover { background: #5e4edb; }
QPushButton#danger { color: #ff8d94; border-color: #62353b; }
QPushButton#nav { text-align: left; border: none; background: transparent; color: #9aa4b4; padding: 10px 14px; }
QPushButton#nav:hover { background: #1a202b; color: white; }
QPushButton#nav:checked { background: #24233d; color: #a99fff; border-left: 3px solid #7868ff; }
QPushButton:focus, QPushButton#primary:focus, QPushButton#nav:focus, QPushButton#danger:focus,
QCheckBox:focus, QTableWidget:focus { border: 2px solid #a99fff; }
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit { background: #0f131a; border: 1px solid #303846; border-radius: 8px; padding: 8px; selection-background-color: #6d5dfc; }
QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border-color: #7868ff; }
QComboBox QAbstractItemView { background: #171c25; selection-background-color: #35305f; border: 1px solid #313947; }
QTableWidget { background: #11161e; alternate-background-color: #151b24; border: 1px solid #242c38; border-radius: 9px; gridline-color: #242c38; }
QHeaderView::section { background: #1b212b; color: #aeb7c5; border: none; border-bottom: 1px solid #303744; padding: 9px; }
QProgressBar { background: #171c24; border: none; border-radius: 5px; height: 10px; text-align: center; color: transparent; }
QProgressBar::chunk { background: #7566ff; border-radius: 5px; }
QWidget#downloadView, QWidget#downloadView QLabel { background: transparent; }
QFrame#transferIndicator { background: #9f90ff; border-radius: 3px; }
QLabel#transferStatus { color: #e6e9ef; font-weight: 600; }
QLabel#transferSummary, QLabel#transferMetric, QLabel#transferPercent { font-family: "Consolas", "Courier New"; }
QLabel#transferSummary { color: #a9b2c5; }
QLabel#transferMetricLabel { color: #a9b2c5; font-size: 12px; }
QLabel#transferMetric { color: #e6e9ef; font-size: 14px; }
QLabel#transferPercent { min-width: 88px; color: #c5baff; font-size: 23px; font-weight: 700; }
QProgressBar#downloadTransfer { background: #25243a; border-radius: 4px; }
QProgressBar#downloadTransfer::chunk { background: #8876ff; border-radius: 4px; }
QPushButton#cancelDownload { color: #f4a1aa; background: transparent; border: 1px solid #774a55; padding: 6px 10px; }
QPushButton#cancelDownload:hover { background: #35232d; border-color: #b26979; }
QPushButton#cancelDownload:pressed { background: #452731; border-color: #d58a99; }
QPushButton#cancelDownload:focus { border-color: #f4a1aa; }
QPushButton#cancelDownload:disabled { border-color: #503940; color: #9b737d; }
QFrame#captionCard { background: #161a25; border: 1px solid #2a3040; border-radius: 12px; }
QFrame#captionCard:hover { border-color: #4a4261; }
QFrame#captionCard[playing="true"] { border-color: #665798; }
QFrame#captionCard[selected="true"] { background: #1d1a2c; border-color: #9b82ff; }
QLabel#captionTitle { background: transparent; color: #f5f1ff; font-size: 15px; font-weight: 700; }
QLabel#captionDescription { background: transparent; color: #aeb5c8; }
QProgressBar#captionPreviewProgress { background: #2b2f42; border-radius: 1px; }
QProgressBar#captionPreviewProgress::chunk { background: #a48dff; border-radius: 1px; }
QPushButton#captionPreviewPlay, QPushButton#captionPreviewReplay, QPushButton#captionSelect { padding: 5px 5px; }
QPushButton#captionPreviewReplay { font-size: 15px; }
QPushButton#captionSelect[selected="true"] { background: #32284f; color: #dfd4ff; border-color: #6d56b4; }
QPushButton#captionSelect[selected="true"]:hover { background: #3a2e5b; }
QPushButton#captionSelect[selected="true"]:focus { border-color: #c5baff; }
QCheckBox { spacing: 8px; }
QCheckBox::indicator { width: 17px; height: 17px; }
QScrollBar:vertical { width: 9px; background: transparent; }
QScrollBar::handle:vertical { background: #333c4b; min-height: 28px; border-radius: 4px; }
QSlider::groove:horizontal { height: 5px; background: #282e39; border-radius: 2px; }
QSlider::handle:horizontal { width: 15px; margin: -5px 0; background: #7b6cff; border-radius: 7px; }
"""

