from dataclasses import dataclass


@dataclass
class RedHerringSpec:
    noise_service: str
    noise_keywords: list
    noise_metric: str
    noise_value: float


class RedHerringInjector:
    def inject(self, evidence: dict, spec: RedHerringSpec) -> dict:
        injected = dict(evidence)
        noise = {
            "service": spec.noise_service,
            "alert": f"{spec.noise_metric} = {spec.noise_value}",
            "keywords": spec.noise_keywords,
            "note": "appears correlated but is unrelated to root cause",
        }
        injected["_red_herring_noise"] = noise
        return injected

    def extract_noise_keywords(self, spec: RedHerringSpec) -> list[str]:
        return spec.noise_keywords + [spec.noise_service, spec.noise_metric]
