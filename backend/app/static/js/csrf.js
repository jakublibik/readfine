document.addEventListener("htmx:configRequest", (e) => {
    const token = document.cookie.split("; ").find(r => r.startsWith("csrftoken="))?.split("=")[1];
    if (token) e.detail.headers["x-csrftoken"] = token;
});
