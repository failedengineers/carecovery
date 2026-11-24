(() => {
  const menu = document.getElementById('menuToggle');
  const sidebar = document.getElementById('sidebar');
  if (menu && sidebar) menu.addEventListener('click', () => sidebar.classList.toggle('open'));

  const date = document.getElementById('todayLabel');
  if (date) date.textContent = new Intl.DateTimeFormat('en-IN', { weekday: 'short', day: '2-digit', month: 'short' }).format(new Date());

  const notificationToggle = document.getElementById('notificationToggle');
  const notificationPanel = document.getElementById('notificationPanel');
  if (notificationToggle && notificationPanel) {
    notificationToggle.addEventListener('click', (event) => {
      event.stopPropagation();
      notificationPanel.classList.toggle('open');
    });
    document.addEventListener('click', (event) => {
      if (!notificationPanel.contains(event.target) && !notificationToggle.contains(event.target)) {
        notificationPanel.classList.remove('open');
      }
    });
  }
})();
