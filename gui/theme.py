"""A quiet, legible desktop theme shared by the five application pages."""
STYLE = """
QMainWindow, QDialog, QWizard { background: #10151f; }
QWidget { color: #edf1f7; font-family: 'Segoe UI', sans-serif; font-size: 13px; }
QWidget#sidebar { background: #151c28; border-right: 1px solid #263246; }
QLabel#brand { font-size: 21px; font-weight: 700; }
QLabel#pageTitle { font-size: 28px; font-weight: 700; }
QLabel#sectionTitle, QLabel#cardTitle { font-size: 18px; font-weight: 600; }
QLabel#metricValue { font-size: 25px; font-weight: 650; }
QLabel#muted { color: #9daec3; }
QLabel#tiny { color: #8e9fb5; font-size: 11px; }
QLabel#demoBanner { background: #3b2f1c; color: #ffda8d; padding: 11px 14px; border-radius: 8px; }
QLabel#success { color: #6ad9bb; }
QLabel#warning { color: #f4cc85; }
QLabel#errorMessage { background: #3a212b; border: 1px solid #6a3546; color: #ffd0d9; padding: 12px; border-radius: 8px; }
QLabel#statusBadge { background: #1c3a35; color: #8ce5ca; border-radius: 7px; padding: 6px 10px; }
QFrame#card, QFrame#taskCard, QFrame#metricCard, QFrame#imageCard { background: #192231; border: 1px solid #2b394d; border-radius: 12px; }
QPushButton { background: #253348; border: 1px solid #34445e; border-radius: 7px; padding: 9px 14px; }
QPushButton:hover { background: #30425b; border-color: #567393; }
QPushButton:pressed { background: #172b3c; }
QPushButton:disabled { color: #6f7f94; background: #1c2737; border-color: #2c394b; }
QPushButton#primary { background: #229a7c; border: 1px solid #30b895; color: white; font-weight: 650; }
QPushButton#primary:hover { background: #2caf8d; }
QPushButton#primary:disabled { background: #245b50; color: #93b7ad; border-color: #2a695a; }
QPushButton#nav { text-align: left; padding: 13px 17px; border: 0; background: transparent; color: #b4c1d3; font-size: 14px; }
QPushButton#nav:checked { background: #263e4d; color: #9be7d5; }
QPushButton#nav:hover { background: #202e41; }
QPushButton#linkButton, QToolButton { background: transparent; border: 0; color: #81d8c5; padding: 7px 2px; text-align: left; }
QPushButton#danger { color: #ffbdc7; }
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QDateEdit, QComboBox { background: #121c29; border: 1px solid #35465e; border-radius: 7px; padding: 8px; selection-background-color: #286a73; }
QLineEdit:focus, QPlainTextEdit:focus { border-color: #43bea0; }
QLineEdit#urlInput { font-size: 16px; padding: 14px; }
QComboBox QAbstractItemView { background: #1a293c; selection-background-color: #315267; }
QScrollArea { background: transparent; border: 0; }
QScrollArea > QWidget > QWidget, QStackedWidget > QWidget { background: #10151f; }
QScrollBar:vertical { background: #151e2b; width: 9px; border: 0; }
QScrollBar::handle:vertical { background: #3b4c63; min-height: 30px; border-radius: 4px; }
QScrollBar:horizontal { background: #151e2b; height: 9px; border: 0; }
QScrollBar::handle:horizontal { background: #3b4c63; min-width: 30px; border-radius: 4px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QTableView { background: #162130; alternate-background-color: #1b293a; border: 1px solid #31415a; selection-background-color: #28566a; gridline-color: #2b3a50; }
QHeaderView::section { background: #24364c; color: #bdcadb; border: 0; padding: 8px; }
QTabWidget::pane { border: 1px solid #2c3d53; background: #172232; border-radius: 7px; }
QTabBar::tab { padding: 10px 15px; background: #1c293a; color: #9eb2ca; }
QTabBar::tab:selected { background: #2a4054; color: #98e2d1; }
QCheckBox { spacing: 8px; }
QCheckBox::indicator { width: 17px; height: 17px; }
QProgressBar { border: 0; border-radius: 3px; background: #203147; max-height: 5px; }
QProgressBar::chunk { background: #34ad8c; border-radius: 3px; }
QStatusBar { color: #a6b8cc; background: #151e2b; }
QGroupBox { border: 1px solid #2b3b51; border-radius: 8px; margin-top: 14px; padding: 14px; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; color: #a8bbd0; }
"""
