import asyncio
import threading
from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.textinput import TextInput
from kivy.clock import Clock

class TradeMatrixApp(App):
    def build(self):
        self.title = "TradeMatrix - Tactical Neural Matrix"
        
        layout = BoxLayout(orientation='vertical', padding=20, spacing=15)
        
        self.status_label = Label(
            text="TradeMatrix System: Online\nWaiting for Webhook Signals...",
            font_size=18,
            halign='center',
            valign='middle'
        )
        self.status_label.bind(size=self.status_label.setter('text_size'))
        layout.add_widget(self.status_label)
        
        self.log_input = TextInput(
            text="[System Initialized]\n",
            readonly=True,
            font_size=14,
            background_color=(0.1, 0.1, 0.1, 1),
            foreground_color=(0, 1, 0, 1)
        )
        layout.add_widget(self.log_input)
        
        btn_layout = BoxLayout(size_hint_y=None, height=50, spacing=10)
        
        test_btn = Button(
            text="Test Signal",
            background_color=(0.2, 0.6, 0.8, 1)
        )
        test_btn.bind(on_press=self.trigger_test_signal)
        btn_layout.add_widget(test_btn)
        
        exit_btn = Button(
            text="Exit",
            background_color=(0.8, 0.2, 0.2, 1)
        )
        exit_btn.bind(on_press=self.stop)
        btn_layout.add_widget(exit_btn)
        
        layout.add_widget(btn_layout)
        
        # Start background webhook / async worker thread
        threading.Thread(target=self.start_background_loop, daemon=True).start()
        
        return layout

    def trigger_test_signal(self, instance):
        self.log_input.text += "\n[Manual Test] Signal received successfully!"
        self.status_label.text = "Status: Test Signal Processed"

    def start_background_loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self.async_webhook_listener())

    async def async_webhook_listener(self):
        # Async loop for webhook server operations
        while True:
            await asyncio.sleep(10)
            Clock.schedule_once(lambda dt: self.update_status_from_async("Background Async Tick Active"))

    def update_status_from_async(self, message):
        self.log_input.text += f"\n[{message}]"

if __name__ == '__main__':
    TradeMatrixApp().run()
